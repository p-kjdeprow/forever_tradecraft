from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import multiprocessing
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image, ImageOps

from crowley.native import build as native_build
from crowley.native import matcher as matcher_runtime
from crowley.native import pins
from crowley.session import pointer_execution
from crowley.session.executor import BindingMetadata, Point, PointerTarget, Rect

MODULE_PATH = Path(__file__).parents[1] / "tools" / "native_calibration_operator.py"
sys.path.insert(0, str(MODULE_PATH.parent))
spec = importlib.util.spec_from_file_location("native_calibration_operator", MODULE_PATH)
assert spec and spec.loader
operator = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = operator
spec.loader.exec_module(operator)

_TEST_XDOTOOL_BUILD_INPUTS = {
    "compiler": {"name": "cc", "sha256": "0" * 64, "version": "test compiler"},
    "linker": {"name": "ld", "sha256": "1" * 64, "version": "test linker"},
}


@pytest.fixture(autouse=True)
def _writable_pointer_rate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Give operator integration tests a writable rate-lock root."""

    state = tmp_path / "pointer-rate-state"

    def state_directory() -> Path:
        state.mkdir(mode=0o700, exist_ok=True)
        return state

    monkeypatch.setattr(pointer_execution, "_session_state_directory", state_directory)


# These are small grayscale chat-only crops derived from the retained GUI
# evidence.  They contain the fixed editor rectangle, not a desktop frame or
# private client state, and keep this regression independent of private paths.
_OCR_CLOSED_CROP_B64 = """\n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
gg==\n"""
_OCR_TYPED_CROP_B64 = """\n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\n"""


# Additional retained command-body evidence for the short fixed /reload form.
_OCR_RELOAD_CLOSED_CROP_B64 = """\n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\n"""
_OCR_RELOAD_TYPED_CROP_B64 = """\n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
hUR2AAAAAElFTkSuQmCC\n"""

# This sanitized failed /reload crop is the exact 460x37 grayscale region. The
# original failed fullframe was unavailable; no desktop, identity, or chat
# history is retained in this self-contained fixture.
_OCR_FAILED_RELOAD_SANITIZED_CROP_B64 = """\niVBORw0KGgoAAAANSUhEUgAAAcwAAAAlCAAAAADqjIakAAAC3klEQVR4nO2Wv2tTURTHP+clMRGivIeTuDTFUZBodFAQWn+NUiv+AVJ0FRFcBRWk0Ek30UVwsYii6KCmS0fbVHAQ0Saugk3EwR/vJdchrfTHTXNf0mcdzmdJ3n3n3HPO/d5z7wNFURRFURRFUZRNR1wN5zIhKcALM6EHSEQawCPM/E5H6aa+T+q9AOmQTAR4EJGO0rKnHzG3tH/8hovxL7Ku8yo94f20Dcac5IVZ/jT83GJy+UbMOZUNIu1quNTC0ZYVg1FmraVpgTi3/P+A6W5i5W+RxjKWKNYoMcUsVkQCP/DLDPtTdUQQhv1yAzgdlKvAaF0gfsc7pLrBLBfQJsq6rE6wY8KxKulovDatls3MXUwBOFr2vH23CrOvHxTmrx/+JuA9Deq3D3/mXbV++9BbpjHBZDuv5BSJObNdIbGZyNqhuGF7Kbubj7A6H6uHcwe1t8LQSw8KO/YfKR44+/CqAUaC0vE7F1tDlWNnx0+1RigdWFgM2+vZ1R1jkpu7X5Lawi4VO3dmCoDgaxaqC1tPVkcz34+FQHGS3JP70dSr3QdLM1FxktzjbUvxEzwtDa7r1qPuFreu9Wz2d4KzmAAMVBGoiwSF88JsCwg+pqgFTf9ZYRaf4FOKxvYlc5NsecZh+XptYavfumr2UepGrVI8MYfK0g5ebV6QYNAD6oMwUONStWRuBlQL4C+/eBLerOvr6a6krDLt4NmpnM1uyUXifXXue7NoP3Vmd278Qgp4NOZz7bWwIDvPC4/GfP9czG7o8/6zX6DG9HOxxvT8F1q6xHDuTCNAcaZ9dVKZ+FCvnWgCcxNf6pUr3r3pYbm7l9rEl0b5ny/FyuM8yY8je2smff4AIN0Lc04j5wED73NAM8xBmG9IVppRljDfkBytXfOkTJZ82PC83+T7yTt2cv1gLH87L5us8xQHu6d9q9gGmz96Dq0oiqIoiqIoiqIoiqIoiqIoiqIoiqIoiqIoffAHe68ZV7fPRuQAAAAASUVORK5CYII=\n"""

# Fresh active-empty and inactive world-texture evidence, reduced to the exact
# 460x37 grayscale chat region. No desktop frame, identity, or chat history is
# retained in these self-contained fixtures.

_OCR_ACTIVE_EMPTY_CROP_B64 = """\n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
AAAASUVORK5CYII=\n"""
_OCR_WORLD_CLOSED_CROP_B64 = """\n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
krmui+P/A4fERRG4/ZQxAAAAAElFTkSuQmCC\n"""


def _write_chat_ocr_fixture(tmp_path: Path, name: str, encoded: str) -> Path:
    path = tmp_path / name
    path.write_bytes(base64.b64decode(encoded))
    path.chmod(0o600)
    return path


def _legacy_chat_ocr_without_contrast(path: Path, destination: Path) -> str:
    with Image.open(path) as image_raw:
        image = image_raw.convert("L")
        left, top, width, height = operator.CHAT_OCR_BOX
        prepared = image.crop((left, top, left + width, top + height))
        foreground_probe = prepared.point(lambda value: 255 if value > operator.CHAT_OCR_THRESHOLD else 0)
        for column in range(width):
            foreground = sum(foreground_probe.getpixel((column, row)) // 255 for row in range(height))
            if foreground >= height - 1:
                for row in range(height):
                    prepared.putpixel((column, row), 0)
        prepared = prepared.resize(
            (width * operator.CHAT_OCR_SCALE, height * operator.CHAT_OCR_SCALE),
            Image.Resampling.BICUBIC,
        )
        prepared_path = destination / "legacy-ocr.png"
        prepared.save(prepared_path, format="PNG")
    result = subprocess.run(
        ["tesseract", str(prepared_path), "stdout", "--psm", "7"],
        check=True,
        capture_output=True,
        text=True,
    )
    return operator.normalize_ocr(result.stdout)


def _chat_ocr_with_cursor_threshold(path: Path, destination: Path, threshold: int) -> str:
    """Reproduce the production preprocessing with one cursor threshold."""
    with Image.open(path) as image_raw:
        image = image_raw.convert("L")
        left, top, width, height = operator.CHAT_OCR_BOX
        prepared = ImageOps.autocontrast(image.crop((left, top, left + width, top + height)), cutoff=1)
        foreground_probe = prepared.point(lambda value: 255 if value > threshold else 0)
        for column in range(width):
            foreground = sum(foreground_probe.getpixel((column, row)) // 255 for row in range(height))
            if foreground >= height - 1:
                for row in range(height):
                    prepared.putpixel((column, row), 0)
        prepared = prepared.resize(
            (width * operator.CHAT_OCR_SCALE, height * operator.CHAT_OCR_SCALE),
            Image.Resampling.BICUBIC,
        )
        prepared_path = destination / f"threshold-{threshold}.png"
        prepared.save(prepared_path, format="PNG")
    result = subprocess.run(
        ["tesseract", str(prepared_path), "stdout", "--psm", "7"],
        check=True,
        capture_output=True,
        text=True,
        timeout=2.0,
        env={**os.environ, "OMP_THREAD_LIMIT": "1"},
    )
    return operator.normalize_ocr(result.stdout)


def _synthetic_chat_state(_path: Path) -> str:
    return operator.CHAT_EDITOR_INACTIVE


def _minimal_chat_ocr_operator(tmp_path: Path, closed: Path, typed: Path):
    instance = object.__new__(operator.NativeCalibrationOperator)
    instance._phase_deadlines = {}
    instance._release_dispatch = None
    instance._history_before_action = 0
    instance._state_history = []
    instance.clock = time.monotonic
    deadline = time.monotonic() + 30.0
    instance._deadline = lambda phase=None: deadline
    instance._recheck = lambda expected_state, deadline_monotonic: None
    instance._identity_guard = lambda deadline_monotonic: None
    instance._event = lambda name, **fields: None
    instance.input = FakeInput(None)
    instance.capture_records = []

    def capture(label, **kwargs):
        source = closed if "chat-closed" in label else typed
        target = tmp_path / f"{label}.png"
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
        instance.capture_records.append(
            {"label": label, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
        )
        return target, None, None

    instance._capture = capture
    instance.ocr = operator.NativeCalibrationOperator._ocr_file
    instance.chat_state = operator.NativeCalibrationOperator._chat_editor_state
    return instance


def test_production_chat_ocr_preserves_typed_command_and_documents_inactive_text(tmp_path):
    # Uniform contrast can make a darker inactive body look like OCR text. The
    # state guard must classify it as inactive before production calls OCR.
    closed = _write_chat_ocr_fixture(tmp_path, "closed.png", _OCR_CLOSED_CROP_B64)
    typed = _write_chat_ocr_fixture(tmp_path, "typed.png", _OCR_TYPED_CROP_B64)
    assert Image.open(closed).size == operator.CHAT_CROP_SIZE
    assert Image.open(typed).size == operator.CHAT_CROP_SIZE
    assert operator.NativeCalibrationOperator._chat_editor_state(closed) == operator.CHAT_EDITOR_INACTIVE
    assert operator.normalize_ocr(operator.NativeCalibrationOperator._ocr_file(closed)) == ""
    assert (
        operator.normalize_ocr(operator.NativeCalibrationOperator._ocr_file(typed))
        == "/crowley agent recover"
    )
    malformed = tmp_path / "malformed.png"
    Image.new("L", (operator.CHAT_CROP_SIZE[0] - 1, operator.CHAT_CROP_SIZE[1])).save(malformed, format="PNG")
    with pytest.raises(operator.OperatorError, match="crop geometry is malformed"):
        operator.NativeCalibrationOperator._ocr_file(malformed)


def test_production_chat_ocr_preserves_reload_command_body(tmp_path):
    closed = _write_chat_ocr_fixture(tmp_path, "reload-closed.png", _OCR_RELOAD_CLOSED_CROP_B64)
    typed = _write_chat_ocr_fixture(tmp_path, "reload-typed.png", _OCR_RELOAD_TYPED_CROP_B64)
    assert operator.normalize_ocr(operator.NativeCalibrationOperator._ocr_file(closed)) == ""
    assert operator.normalize_ocr(operator.NativeCalibrationOperator._ocr_file(typed)) == "/reload"


def test_production_chat_ocr_normalizes_sanitized_failed_reload_texture(tmp_path):
    failed = _write_chat_ocr_fixture(
        tmp_path, "failed-reload-sanitized.png", _OCR_FAILED_RELOAD_SANITIZED_CROP_B64
    )
    assert Image.open(failed).mode == "L"
    assert _legacy_chat_ocr_without_contrast(failed, tmp_path) == "reload"
    assert operator.normalize_ocr(operator.NativeCalibrationOperator._ocr_file(failed)) == "/reload"


def test_actual_failed_reload_crop_distinguishes_cursor_threshold_regression(tmp_path):
    failed = Path(__file__).parent / "fixtures" / "native-chat-ocr" / "failed-reload-actual.png"
    assert hashlib.sha256(failed.read_bytes()).hexdigest() == (
        "269634eb9956eec19ab2ff63e19df4798db7c44ed9edd50381e4b57dc06189d6"
    )
    with Image.open(failed) as image:
        # The retained production crop is RGB; _ocr_file normalizes it to L.
        assert image.mode == "RGB"
        assert image.size == operator.CHAT_CROP_SIZE
    # The retained production crop fails with the old probe: it erases the
    # antialiased slash column and Tesseract reports a leading ``l``.
    assert _chat_ocr_with_cursor_threshold(failed, tmp_path, operator.CHAT_OCR_THRESHOLD) == "lreload"
    assert _chat_ocr_with_cursor_threshold(failed, tmp_path, operator.CHAT_CURSOR_THRESHOLD) == "/reload"
    assert operator.normalize_ocr(operator.NativeCalibrationOperator._ocr_file(failed)) == "/reload"


def test_chat_editor_state_classifies_fresh_active_and_inactive_crops(tmp_path):
    active = _write_chat_ocr_fixture(tmp_path, "active-empty.png", _OCR_ACTIVE_EMPTY_CROP_B64)
    world = _write_chat_ocr_fixture(tmp_path, "world-closed.png", _OCR_WORLD_CLOSED_CROP_B64)
    closed = _write_chat_ocr_fixture(tmp_path, "closed.png", _OCR_CLOSED_CROP_B64)
    for path in (active, world, closed):
        with Image.open(path) as image:
            assert image.mode == "L"
            assert image.size == operator.CHAT_CROP_SIZE
    assert operator.NativeCalibrationOperator._chat_editor_state(active) == operator.CHAT_EDITOR_ACTIVE
    assert operator.NativeCalibrationOperator._chat_editor_state(world) == operator.CHAT_EDITOR_INACTIVE
    assert operator.NativeCalibrationOperator._chat_editor_state(closed) == operator.CHAT_EDITOR_INACTIVE


@pytest.mark.parametrize(
    ("fixture_name", "expected_sha256"),
    [
        (
            "closed-stormwind-preflight-actual.png",
            "60eeaff29a2b590e36aa1d91eebfe7b358a3d3285787226139631fbb5b3886a4",
        ),
        (
            "closed-stormwind-recovery-actual.png",
            "126a3fa37a1a60ca7dc546a87dd23c6cd8b1ca56ed4d4476d075417ba17d83ca",
        ),
    ],
)
def test_actual_stormwind_closed_crops_replay_through_guarded_chat(tmp_path, fixture_name, expected_sha256):
    closed = Path(__file__).parent / "fixtures" / "native-chat-ocr" / fixture_name
    typed = _write_chat_ocr_fixture(tmp_path, "reload-typed.png", _OCR_RELOAD_TYPED_CROP_B64)
    assert hashlib.sha256(closed.read_bytes()).hexdigest() == expected_sha256
    assert operator.NativeCalibrationOperator._chat_editor_state(closed) == operator.CHAT_EDITOR_INACTIVE

    instance = _minimal_chat_ocr_operator(tmp_path, closed, typed)
    sequence = []
    instance._event = lambda name, **fields: sequence.append(("event", name, fields))
    original_key_return = instance.input.key_return

    def record_key_return(*, deadline_monotonic=None):
        sequence.append(("input", "Return", {}))
        original_key_return(deadline_monotonic=deadline_monotonic)

    instance.input.key_return = record_key_return
    instance._chat("/reload")

    assert [record["sha256"] for record in instance.capture_records] == [
        expected_sha256,
        hashlib.sha256(typed.read_bytes()).hexdigest(),
    ]
    assert instance.input.calls == [
        ("key", "Return"),
        ("type", "/reload"),
        ("key", "Return"),
    ]
    accepted = next(
        index
        for index, item in enumerate(sequence)
        if item[0] == "event" and item[1] == "chat_readback" and item[2]["accepted"]
    )
    returns = [index for index, item in enumerate(sequence) if item[:2] == ("input", "Return")]
    assert len(returns) == 2
    assert returns[0] < accepted < returns[1]


def test_chat_active_empty_refuses_before_ocr_or_input(tmp_path):
    active = _write_chat_ocr_fixture(tmp_path, "active-empty.png", _OCR_ACTIVE_EMPTY_CROP_B64)
    typed = _write_chat_ocr_fixture(tmp_path, "typed.png", _OCR_RELOAD_TYPED_CROP_B64)
    instance = _minimal_chat_ocr_operator(tmp_path, active, typed)

    def fail_ocr(_path):
        raise AssertionError("active editor must refuse before command OCR")

    instance.ocr = fail_ocr
    with pytest.raises(operator.OperatorError, match="chat editor is active"):
        instance._chat("/reload")
    assert instance.input.calls == []


def test_chat_inactive_world_texture_does_not_become_command_text(tmp_path):
    world = _write_chat_ocr_fixture(tmp_path, "world-closed.png", _OCR_WORLD_CLOSED_CROP_B64)
    typed = _write_chat_ocr_fixture(tmp_path, "typed.png", _OCR_RELOAD_TYPED_CROP_B64)
    instance = _minimal_chat_ocr_operator(tmp_path, world, typed)
    instance._chat("/reload")
    assert instance.input.calls == [
        ("key", "Return"),
        ("type", "/reload"),
        ("key", "Return"),
    ]


def test_chat_unknown_or_malformed_state_refuses_before_ocr_or_input(tmp_path):
    typed = _write_chat_ocr_fixture(tmp_path, "typed.png", _OCR_RELOAD_TYPED_CROP_B64)
    black = tmp_path / "black.png"
    Image.new("L", operator.CHAT_CROP_SIZE, 0).save(black, format="PNG")
    malformed = tmp_path / "malformed-state.png"
    Image.new("L", (operator.CHAT_CROP_SIZE[0] - 1, operator.CHAT_CROP_SIZE[1]), 0).save(
        malformed, format="PNG"
    )
    for closed in (black, malformed):
        instance = _minimal_chat_ocr_operator(tmp_path, closed, typed)
        instance.ocr = lambda _path: (_ for _ in ()).throw(
            AssertionError("unknown editor state must refuse before command OCR")
        )
        with pytest.raises(operator.OperatorError, match="(?:unknown|malformed|unavailable)"):
            instance._chat("/reload")
        assert instance.input.calls == []


def test_chat_mixed_chrome_signature_refuses_as_unknown_before_ocr_or_input(tmp_path):
    mixed = _write_chat_ocr_fixture(tmp_path, "mixed.png", _OCR_CLOSED_CROP_B64)
    with Image.open(mixed) as image:
        image = image.convert("L")
        for row in (7, 23):
            for column in range(40, 440):
                image.putpixel((column, row), 210)
        image.save(mixed, format="PNG")
    typed = _write_chat_ocr_fixture(tmp_path, "typed.png", _OCR_RELOAD_TYPED_CROP_B64)
    instance = _minimal_chat_ocr_operator(tmp_path, mixed, typed)
    instance.ocr = lambda _path: (_ for _ in ()).throw(
        AssertionError("mixed visual state must refuse before command OCR")
    )
    with pytest.raises(operator.OperatorError, match="^chat editor visual state is unknown$"):
        instance._chat("/reload")
    assert instance.input.calls == []


def test_chat_closed_guard_rejects_nonempty_actual_ocr(tmp_path):
    closed = _write_chat_ocr_fixture(tmp_path, "closed.png", _OCR_CLOSED_CROP_B64)
    typed = _write_chat_ocr_fixture(tmp_path, "typed.png", _OCR_TYPED_CROP_B64)
    instance = _minimal_chat_ocr_operator(tmp_path, typed, typed)
    with pytest.raises(operator.OperatorError, match="chat editor is active"):
        instance._chat("/crowley agent recover")
    assert instance.input.calls == []

    instance = _minimal_chat_ocr_operator(tmp_path, closed, typed)
    instance._chat("/crowley agent recover")
    assert instance.input.calls == [
        ("key", "Return"),
        ("type", "/crowley agent recover"),
        ("key", "Return"),
    ]


@dataclass(frozen=True)
class Identity:
    fingerprint: str = "a" * 64
    child_geometry: tuple[int, int, int, int] = (0, 0, 1280, 720)
    target_event_xids: frozenset[int] = frozenset({1})


def _movement_gesture(identity: Identity, *, start: tuple[int, int] = (20, 20), seed: int = 7):
    geometry = identity.child_geometry
    child_xid = next(iter(identity.target_event_xids))
    binding = BindingMetadata(
        identity.fingerprint,
        f"movement-{seed}",
        1,
        1,
        child_xid,
        (child_xid,),
        source="test-native-parking",
    )
    target = PointerTarget("parking", Rect(635, 355, 11, 11), binding, padding=2)
    return pointer_execution.plan_human_pointer(
        Point(*start),
        target,
        Rect(0, 0, geometry[2], geometry[3]),
        seed=seed,
        button=None,
    )


def _click_gesture(match: Any, identity: Identity, *, seed: int = 11):
    geometry = identity.child_geometry
    child_xid = next(iter(identity.target_event_xids))
    binding = BindingMetadata(
        identity.fingerprint,
        match.capture_sha256,
        1,
        1,
        child_xid,
        (child_xid,),
        source="test-native-click",
    )
    return pointer_execution.plan_human_pointer(
        Point(20, 20),
        PointerTarget("test-button", Rect(*match.box), binding, padding=1),
        Rect(0, 0, geometry[2], geometry[3]),
        seed=seed,
    )


class FakeScreen:
    def __init__(self, root: Path, construct: Path, restore: Path):
        self.root = root
        self.construct = construct
        self.restore = restore
        self.identity = Identity()
        self.last_point = (0, 0, 1)
        self.chat_crop = (0, 0, 64, 64)
        self.agent_crop = (0, 0, 64, 64)

    def wait_reload_boundary(self, *, deadline_monotonic=None, proof="ordinary"):
        return self.identity

    def resolve_identity(self, *, require_foreground: bool = True, deadline_monotonic=None):
        assert require_foreground
        return self.identity

    def capture(self, path: Path, *, identity: Identity, deadline_monotonic=None):
        assert identity is self.identity
        if "post-reload-controls" in path.name and hasattr(self, "dual"):
            source = self.dual
        else:
            source = self.restore if "restore-control" in path.name else self.construct
        path.write_bytes(source.read_bytes())
        os.chmod(path, 0o600)
        return self.identity

    def pointer_position(self, *, deadline_monotonic=None):
        return self.last_point


class FakeInput:
    def __init__(self, screen: FakeScreen, ocr_text: str = "", dispatch=None):
        self.screen = screen
        self.ocr_text = ocr_text
        self.dispatch = dispatch
        self.pending_command = None
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def key_return(self, *, deadline_monotonic=None):
        self.calls.append(("key", "Return"))
        if self.pending_command is not None and self.dispatch is not None:
            self.dispatch(self.pending_command)
            self.pending_command = None

    def key_escape(self, *, deadline_monotonic=None):
        self.calls.append(("key", "Escape"))

    def type_command(self, command: str, *, deadline_monotonic=None):
        self.calls.append(("type", command))
        self.pending_command = command

    def move_pointer(self, gesture, *, identity, before_press=None, deadline_monotonic=None):
        left, top, width, height = identity.child_geometry
        point = (left + int(gesture.endpoint.x), top + int(gesture.endpoint.y))
        assert left <= point[0] < left + width
        assert top <= point[1] < top + height
        if before_press is not None:
            before_press()
        self.calls.append(("move", point))
        self.screen.last_point = (*point, 1)

    def click(
        self,
        match,
        *,
        identity,
        gesture,
        rate_permit=None,
        before_press=None,
        deadline_monotonic=None,
    ):
        self.calls.append(("click", match.role))
        left, top, _width, _height = identity.child_geometry
        self.screen.last_point = (
            left + int(gesture.endpoint.x),
            top + int(gesture.endpoint.y),
            1,
        )
        if before_press is not None:
            before_press()
        if self.dispatch is not None:
            self.dispatch(match.role)
        return None

    def close(self):
        self.closed = True


@dataclass
class FakeBinding:
    reservation_id: str = "reservation-1"
    kind: str = "measure"
    state: str = "prepared"

    def record(self, generation: int) -> dict:
        return {
            "reservationId": self.reservation_id,
            "reservationKind": self.kind,
            "reservationState": self.state,
            "controllerGeneration": generation,
            "desktopLeaseHeld": True,
            "guardianGeneration": 0,
            "guardianArmed": False,
            "ownerPid": 1234,
            "ownerProcessStart": "start-1",
            "profileFingerprint": "b" * 64,
            "identityFingerprint": "a" * 64,
            "B0": "boot-1",
            "expectedConstructorBoot": "native-construction:boot-1",
            "expiresMonotonic": 1000000000000.0,
        }


class FakeClient:
    # Explicit test-only supplement for workflows that do not provide the
    # production local SessionIPC socket.  Production clients may never use
    # this bypass.
    _allow_cancellation_actor_bypass = True

    def __init__(
        self,
        generation: int,
        states: list[str],
        *,
        kind: str = "measure",
        recovery_kind: str = "ordinary_reload",
    ):
        self.generation = generation
        self.states = list(states)
        self.kind = kind
        self.recovery_kind = recovery_kind
        self.binding = FakeBinding(kind=kind)
        self.calls: list[dict] = []
        self.history: list[dict] = []
        self.calibration_status_calls = 0
        self.current_state = "construction_prepared"

    def _reply(self, state: str, *, binding: FakeBinding | None = None) -> dict:
        item = binding or self.binding
        if state == "running" and item.kind == "measure":
            item.state = "consumed"
        if state == "finalized":
            item.state = "finalized"
        if not self.history or self.history[-1]["state"] != state:
            self.history.append({"state": state, "nativeClock": float(len(self.history) + 1)})
        handshake = {"state": state, "stateHistory": list(self.history)}
        if state in {
            "construction_released",
            "hold_required",
            "updates_reset_archived",
            "measure_ready",
            "resume_required",
            "resume_ready",
            "finalize_required",
            "finalized",
        }:
            handshake.update(
                {
                    "operationDeadline": 1000000000000.0,
                    "providerDeadline": 1000000000000.0,
                    "constructorClickDeadline": 1000000000000.0,
                }
            )
        if state in {"finalize_required", "finalized"}:
            handshake["finalizeDeadline"] = 1000000000000.0
        return {
            "ok": True,
            "reservation": item.record(self.generation),
            "handshake": handshake,
        }

    def dispatch(self, action: str) -> None:
        transitions = {
            "/crowley agent defer": "construction_prepared",
            "/reload": "construction_prepared",
            "/crowley agent release": "construction_released",
            "construct": "hold_required",
            "/crowley agent hold": "measure_ready",
            "/crowley agent measure": "resume_required",
            "/crowley agent resume": "resume_ready",
            "/crowley agent finalize": "finalized",
            "restore": "recovered",
        }
        target = transitions.get(action)
        if target is None:
            return
        if action == "/crowley agent hold":
            self._reply("updates_reset_archived")
        self.current_state = target

    def prepare(self, generation: int, *, preflight: bool = False):
        assert generation == self.generation
        self.calls.append({"command": "prepare", "preflight": preflight})
        if preflight:
            self.binding = FakeBinding(kind="preflight")
        self.current_state = "construction_prepared"
        return self._reply(self.current_state)

    def bootstrap(self, generation: int, reservation):
        self.calls.append({"command": "bootstrap", "expected": reservation.expected_constructor_boot})
        self.binding.state = "consumed"
        self.current_state = "running"
        return self._reply(self.current_state)

    def native_calibrate(self, generation: int, *, deadline=None):
        assert generation == self.generation
        self.calls.append({"command": "native_calibrate", "generation": generation})
        # The real native worker publishes the reset/baseline barriers before
        # exposing the release gate.  Keep this fixture event driven while
        # retaining that durable history prefix; the operator must observe the
        # new suffix rather than accepting a synthetic recheck progression.
        self._reply("construction_reset_archived")
        self._reply("baseline_bound")
        self._reply("construction_released")
        self.current_state = "construction_released"
        return {
            "ok": True,
            "status": {
                "state": "NATIVE_CALIBRATION",
                "generation": generation,
                "calibrationOperation": {
                    "state": "running",
                    "kind": "native",
                    "generation": generation,
                    "error": "",
                },
            },
        }

    def calibration_status(self, *, deadline=None):
        self.calibration_status_calls += 1
        self.calls.append({"command": "calibration_status"})
        return {
            "ok": True,
            "status": {
                # The real daemon exposes INPUT_CALIBRATION only after the
                # finalized construction handshake has been committed.
                "state": "INPUT_CALIBRATION",
                "generation": self.generation,
                "calibrationOperation": {
                    "state": "complete" if self.calibration_status_calls > 0 else "running",
                    "kind": "native",
                    "generation": self.generation,
                    "error": "",
                },
            },
        }

    def recheck(self, generation: int, reservation_id: str, *, deadline: float | None = None):
        assert generation == self.generation
        assert reservation_id == self.binding.reservation_id
        self.calls.append({"command": "recheck", "reservationId": reservation_id})
        if self.states:
            state = self.states.pop(0)
            return self._reply(state)
        return self._reply(self.current_state)

    def finalize_admit(self, generation: int, reservation_id: str):
        self.calls.append({"command": "finalize_admit", "reservationId": reservation_id})
        self.current_state = "finalize_required"
        return self._reply(self.current_state)

    def recovery_begin(self, generation: int, reservation_id: str = ""):
        self.binding = FakeBinding(reservation_id="ticket-1", kind="recovery", state="recovery")
        self.history = []
        self.current_state = "recovery_required"
        self.calls.append({"command": "recovery_begin", "reservationId": reservation_id})
        return {
            "ok": True,
            "reservation": self.binding.record(self.generation),
            "handshake": {
                "state": self.current_state,
                "recoveryKind": self.recovery_kind,
                "stateHistory": [{"state": "recovery_required", "nativeClock": 1.0}],
            },
        }

    def recovery_complete(self, generation: int, reservation_id: str):
        self.calls.append({"command": "recovery_complete", "reservationId": reservation_id})
        self.binding.state = "recovered"
        self.history = [{"state": "recovery_required", "nativeClock": 1.0}]
        self.current_state = "recovered"
        return self._reply(self.current_state)

    def recovery_cancel(self, generation: int, reservation_id: str):
        self.calls.append({"command": "recovery_cancel", "reservationId": reservation_id})
        self.binding.state = "cancelled"
        self.history = [{"state": "recovery_required", "nativeClock": 1.0}]
        return self._reply("cancelled")

    def status(self, *, deadline=None):
        return {
            "ok": True,
            "status": {
                "state": "PAUSED",
                "generation": self.generation,
                "calibrationOperation": {
                    "state": "error",
                    "kind": "native",
                    "generation": self.generation,
                    "error": "",
                },
            },
        }

    def pause(self, generation: int, *, deadline=None):
        self.calls.append({"command": "pause", "generation": generation})
        return self.status()


class FakeWatchdog:
    def __init__(self):
        self.states: list[dict] = []
        self.closed = False

    def publish(self, *, groups, buttons, keys=frozenset()):
        self.states.append({"groups": set(groups), "buttons": set(buttons), "keys": set(keys)})

    def clear(self):
        self.states.append({"groups": set(), "buttons": set(), "keys": set()})

    def close(self):
        self.closed = True


def _owner_hold_fixture(
    artifact: str, marker: str, action: tuple[str, str], release: tuple[str, str], status
):
    tool = operator.PinnedInputTool.from_root(artifact)
    environment = {"CROWLEY_RECORDER": marker}
    watchdog = operator.Watchdog(
        tool,
        interval=0.02,
        environment=environment,
        owner_pid=os.getpid(),
        owner_start=operator._proc_start(os.getpid()),
    )
    emitter = operator.OwnedEmitter(
        tool,
        action=action,
        release_action=release,
        environment=environment,
    )
    emitter.start()
    assert emitter.pgid and emitter.pgid > 0
    owned_keys = {action[1]} if action[0] == "keydown" else set()
    owned_buttons = {int(action[1])} if action[0] == "mousedown" else set()
    watchdog.publish(groups={emitter.pgid}, buttons=owned_buttons, keys=owned_keys)
    emitter.press()
    assert emitter._process is not None and emitter._process.is_alive()
    status.send((watchdog._process.pid, operator._proc_start(watchdog._process.pid), emitter.pgid))
    while True:
        time.sleep(0.2)


def _production_main_owner_fixture(
    artifact: str,
    release_marker: str,
    gate_marker: str,
    socket_path: str,
    scene_path: str,
    config_path: str,
    templates_path: str,
    error_path: str,
    release_enable: str = "",
    watchdog_info_path: str = "",
):
    # The production helper normally stores receipts below the user's home.
    # Keep this spawned fixture self-contained when the test runner's home is
    # read-only, and record the independent release actor so its eventual
    # reap is asserted by the parent test.
    fixture_home = Path(error_path).parent / "fixture-home"
    fixture_home.mkdir(mode=0o700, exist_ok=True)
    os.environ["HOME"] = str(fixture_home)
    from crowley.session import pointer_execution

    pointer_state = Path(error_path).parent / "fixture-pointer-state"
    pointer_state.mkdir(mode=0o700, exist_ok=True)
    pointer_execution._session_state_directory = lambda: pointer_state

    if watchdog_info_path:
        real_watchdog_init = operator.Watchdog.__init__

        def tracked_watchdog_init(instance, *args, **kwargs):
            real_watchdog_init(instance, *args, **kwargs)
            process = instance._process
            if process is not None and process.pid is not None:
                start = operator._proc_start(process.pid) or ""
                Path(watchdog_info_path).write_text(f"{process.pid} {start}", encoding="utf-8")

        operator.Watchdog.__init__ = tracked_watchdog_init

    class MainConfig:
        socket = Path(socket_path)

    class MainScreen:
        def __init__(self, _config_path, _root):
            self.config_path = Path(config_path)
            self.config = MainConfig()
            self.tool = operator.PinnedInputTool.from_root(Path(artifact))
            self.input_environment = {"CROWLEY_RECORDER": release_marker}
            if release_enable:
                self.input_environment["CROWLEY_RELEASE_ENABLE"] = release_enable
            self.chat_crop = (0, 0, 64, 64)
            self.agent_crop = (0, 0, 64, 64)
            self._pointer_calls = 0

        def resolve_identity(self, *, require_foreground=True, deadline_monotonic=None):
            assert require_foreground
            return Identity()

        def capture(self, path, *, identity, deadline_monotonic=None):
            assert isinstance(identity, Identity)
            shutil.copyfile(scene_path, path)
            os.chmod(path, 0o600)
            return identity

        def pointer_position(self, *, deadline_monotonic=None):
            # The private recorder applies every move from the owned
            # xdotool chain. Before a gesture it remains at the initial point;
            # after movement it reports the exact dispatched endpoint.
            self._pointer_calls += 1
            if self._pointer_calls == 1:
                return (640, 360, 1)
            recorder = Path(self.input_environment["CROWLEY_RECORDER"])
            if recorder.is_file():
                for line in reversed(recorder.read_text(encoding="utf-8").splitlines()):
                    fields = line.split()
                    try:
                        move_index = max(index for index, value in enumerate(fields) if value == "mousemove")
                        return (int(fields[move_index + 1]), int(fields[move_index + 2]), 1)
                    except (ValueError, IndexError):
                        continue
            return (640, 360, 1)

        def close(self):
            self.tool.close()

    class MainClient:
        def __init__(self, socket):
            self.socket_path = Path(socket)
            self.generation = 7
            self.binding = FakeBinding(kind="preflight")
            self.history = [{"state": "construction_prepared", "nativeClock": 1.0}]

        def _reply(self):
            reservation = self.binding.record(self.generation)
            # The helper's own constructor window remains capped at ten
            # seconds from release; leave the fake reservation long enough
            # to test that unchanged phase budget after setup overhead.
            reservation["expiresMonotonic"] = time.monotonic() + 20.0
            return {
                "ok": True,
                "reservation": reservation,
                "handshake": {"state": "construction_prepared", "stateHistory": list(self.history)},
            }

        def prepare(self, generation, *, preflight=False):
            assert preflight and generation == self.generation
            return self._reply()

        def recheck(self, generation, reservation_id, *, deadline=None):
            assert generation == self.generation
            assert reservation_id == self.binding.reservation_id
            return self._reply()

    real_operator = operator.NativeCalibrationOperator

    class DebugOperator(real_operator):
        def __init__(self, *args, **kwargs):
            # This owner fixture uses a 64x64 synthetic screen and exercises
            # emitter lifetime, not the measured chat visual contract.
            kwargs["chat_state"] = _synthetic_chat_state
            super().__init__(*args, **kwargs)

        @staticmethod
        def _ocr_file(path):
            if "chat-closed-" in Path(path).name:
                return ""
            return "/crowley agent defer"

        def run(self):
            try:
                return super().run()
            except BaseException as exc:
                Path(error_path).write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
                raise

    operator.X11Screen = MainScreen
    operator.DaemonClient = MainClient
    operator.NativeCalibrationOperator = DebugOperator
    real_press = operator.OwnedEmitter.press

    def gated_press(emitter):
        if emitter.action[:2] == ("mousedown", "1"):
            Path(gate_marker).write_text("button-watchdog-armed", encoding="utf-8")
        result = real_press(emitter)
        if emitter.action[:2] == ("mousedown", "1"):
            while True:
                time.sleep(0.1)
        return result

    operator.OwnedEmitter.press = gated_press
    operator._source_bindings = lambda _screen, _manifest, _matcher_runtime=None: {}
    raise SystemExit(
        operator.main(
            [
                operator.MODE_CONTROL_PREFLIGHT,
                "--generation",
                "7",
                "--config",
                config_path,
                "--templates",
                templates_path,
            ]
        )
    )


def _persistent_release_owner_fixture(artifact: str, marker: str, enable: str, status):
    tool = operator.PinnedInputTool.from_root(Path(artifact))
    watchdog = operator.Watchdog(
        tool,
        interval=0.02,
        environment={
            "CROWLEY_RECORDER": marker,
            "CROWLEY_RELEASE_ENABLE": enable,
        },
        owner_pid=os.getpid(),
        owner_start=operator._proc_start(os.getpid()),
    )
    watchdog.publish(groups=set(), buttons=set(), keys={"Return"})
    status.send((watchdog._process.pid, operator._proc_start(watchdog._process.pid)))
    while True:
        time.sleep(0.2)


def _normal_exit_failed_release_fixture(artifact: str, marker: str, enable: str, status):
    """Exercise ordinary SystemExit while the independent release is pending."""

    tool = operator.PinnedInputTool.from_root(Path(artifact))
    watchdog = operator.Watchdog(
        tool,
        interval=0.02,
        environment={
            "CROWLEY_RELEASE_MARKER": marker,
            "CROWLEY_RELEASE_ENABLE": enable,
        },
        owner_pid=os.getpid(),
        owner_start=operator._proc_start(os.getpid()),
    )
    watchdog.publish(groups=set(), buttons=set(), keys={"Return"})
    assert watchdog._process is not None
    status.send((watchdog._process.pid, operator._proc_start(watchdog._process.pid)))
    try:
        watchdog.close()
    except operator.OperatorError:
        pass
    raise SystemExit(2)


def _cancellation_owner_fixture(socket_path: str, status):
    actor = operator.CancellationActor(
        Path(socket_path),
        generation=17,
        reservation_id="reservation-live",
        deadline=time.monotonic() + 8.0,
        owner_pid=os.getpid(),
        owner_start=operator._proc_start(os.getpid()),
    )
    actor.start()
    assert actor._process is not None
    status.send((actor._process.pid, operator._proc_start(actor._process.pid)))
    while True:
        time.sleep(0.2)


def _actor_admission_owner_fixture(socket_path: str, status):
    """Real owner process whose actor performs an admission while it is killed."""

    owner_pid = os.getpid()
    owner_start = operator._proc_start(owner_pid)
    actor = operator.CancellationActor(
        Path(socket_path),
        generation=31,
        reservation_id="",
        deadline=time.monotonic() + 12.0,
        owner_pid=owner_pid,
        owner_start=owner_start,
    )
    actor.start()
    assert actor._process is not None
    status.send((owner_pid, owner_start, actor._process.pid, operator._proc_start(actor._process.pid)))
    try:
        actor.call(
            "prepare",
            generation=31,
            preflight=False,
            deadline=time.monotonic() + 10.0,
        )
    except BaseException:
        pass
    while True:
        time.sleep(0.2)


def _peer_pid(connection: socket.socket) -> int:
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, _uid, _gid = struct.unpack("3i", raw)
    return pid


def _admission_reply(
    *,
    owner_pid: int,
    owner_start: str,
    generation: int,
    reservation_id: str,
    kind: str,
    state: str,
    handshake_state: str,
    history: list[str],
    expiry: float | None = None,
    guardian_generation: int = 4,
) -> dict:
    if expiry is None:
        expiry = time.monotonic() + 20.0
    record = {
        "reservationId": reservation_id,
        "reservationKind": kind,
        "reservationState": state,
        "controllerGeneration": generation,
        "desktopLeaseHeld": True,
        "guardianGeneration": guardian_generation,
        "guardianArmed": False,
        "ownerPid": owner_pid,
        "ownerProcessStart": owner_start,
        "profileFingerprint": "b" * 64,
        "identityFingerprint": "a" * 64,
        "B0": "boot-31",
        "expectedConstructorBoot": "native-construction:boot-31",
        "expiresMonotonic": expiry,
    }
    return {
        "ok": True,
        "reservation": record,
        "handshake": {"state": handshake_state},
        "stateHistory": [
            {"state": value, "nativeClock": float(index + 1)} for index, value in enumerate(history)
        ],
    }


def make_template(path: Path, color: tuple[int, int, int]) -> str:
    image = Image.new("RGB", (4, 4), color)
    image.save(path, format="PNG")
    os.chmod(path, 0o600)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wait_pid_identity_exit(pid: int, start: str | None, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = operator._proc_start(pid)
        except Exception:
            current = None
        if current is None or (start is not None and current != start):
            return True
        time.sleep(0.05)
    try:
        return operator._proc_start(pid) is None
    except Exception:
        return True


def _reap_process(process: multiprocessing.Process, timeout: float = 3.0) -> None:
    """Terminate and reap a test-owned process even when assertions fail."""

    if process.is_alive():
        process.kill()
    process.join(timeout=timeout)
    if process.is_alive():
        raise AssertionError(f"test process {process.pid} was not reaped")


def make_scenes(tmp_path: Path):
    construct = tmp_path / "construct.png"
    restore = tmp_path / "restore.png"
    construct_hash = make_template(construct, (255, 0, 0))
    restore_hash = make_template(restore, (0, 255, 0))
    manifest_file = tmp_path / "templates.json"
    manifest_file.write_text(
        json.dumps(
            {
                "construct": {"path": str(construct), "sha256": construct_hash, "threshold": 5.0},
                "restore": {"path": str(restore), "sha256": restore_hash, "threshold": 5.0},
            }
        )
    )
    os.chmod(manifest_file, 0o600)
    scene_construct = Image.new("RGB", (1280, 720), (0, 0, 0))
    scene_restore = Image.new("RGB", (1280, 720), (0, 0, 0))
    scene_construct.paste(Image.open(construct), (1142, 22))
    scene_restore.paste(Image.open(restore), (1211, 22))
    scene_construct_path = tmp_path / "scene-construct.png"
    scene_restore_path = tmp_path / "scene-restore.png"
    scene_construct.save(scene_construct_path, format="PNG")
    scene_restore.save(scene_restore_path, format="PNG")
    return operator.TemplateManifest.load(manifest_file), scene_construct_path, scene_restore_path


def make_recording_artifact(tmp_path: Path) -> Path:
    source = tmp_path / "recorder.c"
    executable = tmp_path / "recorder"
    source.write_text(
        """
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
    const char *path = getenv("CROWLEY_RECORDER");
    if (path == NULL || argc < 3) return 2;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd < 0) return 3;
    dprintf(fd, "%s %s", argv[1], argv[2]);
    if (strcmp(argv[1], "sleep") == 0 || strcmp(argv[1], "mousemove") == 0) {
        for (int i = 3; i < argc; i++) dprintf(fd, " %s", argv[i]);
    }
    dprintf(fd, "\\n");
    close(fd);
    return 0;
}
""",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(source), "-o", str(executable)], check=True)
    from crowley.session.input_tool import (
        ARCHIVE_BASENAME,
        MANIFEST_BASENAME,
        PINNED_ARCHIVE_SHA256,
        PINNED_COMMIT,
        PRIVATE_TOOL_BASENAME,
    )

    artifact = tmp_path / "sealed-tool"
    artifact.mkdir(mode=0o700)
    shutil.copyfile(executable, artifact / PRIVATE_TOOL_BASENAME)
    shutil.copyfile(
        MODULE_PATH.parents[1] / "vendor" / "xdotool-source.tar.gz",
        artifact / ARCHIVE_BASENAME,
    )
    executable_sha256 = hashlib.sha256((artifact / PRIVATE_TOOL_BASENAME).read_bytes()).hexdigest()
    (artifact / MANIFEST_BASENAME).write_text(
        json.dumps(
            {
                "commit": PINNED_COMMIT,
                "archive_sha256": PINNED_ARCHIVE_SHA256,
                "executable": PRIVATE_TOOL_BASENAME,
                "executable_sha256": executable_sha256,
                pins.XDOTOOL_BUILD_INPUTS_KEY: _TEST_XDOTOOL_BUILD_INPUTS,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (artifact / PRIVATE_TOOL_BASENAME).chmod(0o700)
    (artifact / ARCHIVE_BASENAME).chmod(0o600)
    (artifact / MANIFEST_BASENAME).chmod(0o600)
    return artifact


def _consume_action_records(
    pending: bytes,
    chunk: bytes,
    *,
    on_record: Any,
) -> bytes:
    """Consume only complete, positively identified fixture action records."""

    pending += chunk
    while b"\n" in pending:
        raw, pending = pending.split(b"\n", 1)
        if not raw:
            continue
        try:
            fields = raw.decode("ascii").split("\t")
            if len(fields) < 3 or not fields[0].isdecimal() or not fields[1].isdecimal():
                continue
            pid = int(fields[0])
            argc = int(fields[1])
            arguments = fields[2:]
            if pid <= 0 or argc <= 0 or argc != len(arguments):
                continue
        except (UnicodeDecodeError, ValueError):
            continue
        on_record(pid, arguments)
    return pending


def test_action_bridge_buffers_fragments_and_rejects_malformed_records(tmp_path: Path) -> None:
    """A partial recorder row must not dispatch or acknowledge prematurely."""

    action_log = tmp_path / "owned-actions.log"
    action_ack = tmp_path / "owned-action-acks"
    action_ack.mkdir(mode=0o700)
    prefix_written = threading.Event()
    allow_suffix = threading.Event()
    writer_done = threading.Event()
    writer_errors: list[BaseException] = []

    def writer() -> None:
        try:
            with action_log.open("ab") as stream:
                stream.write(b"7001\t2")
                stream.flush()
                prefix_written.set()
                assert allow_suffix.wait(2.0)
                stream.write(b"\tmousedown\t1\n0\t2\tmouseup\t1\n7002\t2\tmouseup\n7003\t2\tmouseup\t1\n")
                stream.flush()
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_done.set()

    applied: list[tuple[int, list[str]]] = []

    def on_record(pid: int, arguments: list[str]) -> None:
        applied.append((pid, arguments))
        (action_ack / str(pid)).touch(mode=0o600)

    producer = threading.Thread(target=writer)
    producer.start()
    assert prefix_written.wait(2.0)
    pending = b""
    with action_log.open("rb") as stream:
        pending = _consume_action_records(pending, stream.read(4096), on_record=on_record)
        offset = stream.tell()
        assert pending == b"7001\t2"
        assert applied == []
        assert list(action_ack.iterdir()) == []
    # The production bridge closes and reopens this file between polls.  The
    # retained pending bytes belong to the parser, while the offset belongs to
    # the underlying file; rewinding it would dispatch the prefix twice.
    allow_suffix.set()
    assert writer_done.wait(2.0)
    with action_log.open("rb") as stream:
        stream.seek(offset)
        pending = _consume_action_records(pending, stream.read(4096), on_record=on_record)
    producer.join(timeout=2.0)
    assert not producer.is_alive()
    assert writer_errors == []
    assert pending == b""
    assert applied == [(7001, ["mousedown", "1"]), (7003, ["mouseup", "1"])]
    assert sorted(path.name for path in action_ack.iterdir()) == ["7001", "7003"]


def make_gated_recording_artifact(tmp_path: Path) -> Path:
    """Build a sealed fixture image that reports each owned action and waits for ACK."""

    source = tmp_path / "gated-recorder.c"
    executable = tmp_path / "gated-recorder"
    source.write_text(
        r"""
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>
int main(int argc, char **argv) {
    const char *log = getenv("CROWLEY_ACTION_LOG");
    const char *acks = getenv("CROWLEY_ACTION_ACK_DIR");
    const char *pointer_state = getenv("CROWLEY_POINTER_STATE");
    if (argc >= 3 && strcmp(argv[1], "getmouselocation") == 0 && strcmp(argv[2], "--shell") == 0) {
        if (!pointer_state) return 2;
        FILE *pointer = fopen(pointer_state, "r");
        if (!pointer) return 2;
        long x = 0, y = 0, window = 0;
        int parsed = fscanf(pointer, "%ld %ld %ld", &x, &y, &window);
        fclose(pointer);
        if (parsed != 3 || x < 0 || y < 0 || window <= 0) return 2;
        dprintf(STDOUT_FILENO, "X=%ld\nY=%ld\nSCREEN=0\nWINDOW=0x%lx\n", x, y, window);
        return 0;
    }
    if (!log || !acks || argc < 2) return 2;
    int fd = open(log, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd < 0) return 3;
    char record[4096];
    int length = snprintf(record, sizeof(record), "%ld\t%d", (long)getpid(), argc - 1);
    if (length < 0 || (size_t)length >= sizeof(record)) return 4;
    for (int i = 1; i < argc; i++) {
        int written = snprintf(record + length, sizeof(record) - (size_t)length, "\t%s", argv[i]);
        if (written < 0 || (size_t)written >= sizeof(record) - (size_t)length) return 4;
        length += written;
    }
    if ((size_t)length + 1 >= sizeof(record)) return 4;
    record[length++] = '\n';
    ssize_t total = write(fd, record, (size_t)length);
    if (total != length) return 5;
    close(fd);
    char ack[4096];
    snprintf(ack, sizeof(ack), "%s/%ld", acks, (long)getpid());
    for (int i = 0; i < 800; i++) {
        if (access(ack, F_OK) == 0) return 0;
        usleep(10000);
    }
    return 4;
}
""",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(source), "-o", str(executable)], check=True)
    from crowley.session.input_tool import (
        ARCHIVE_BASENAME,
        MANIFEST_BASENAME,
        PINNED_ARCHIVE_SHA256,
        PINNED_COMMIT,
        PRIVATE_TOOL_BASENAME,
    )

    artifact = tmp_path / "sealed-gated-tool"
    artifact.mkdir(mode=0o700)
    shutil.copyfile(executable, artifact / PRIVATE_TOOL_BASENAME)
    shutil.copyfile(
        MODULE_PATH.parents[1] / "vendor" / "xdotool-source.tar.gz",
        artifact / ARCHIVE_BASENAME,
    )
    digest = hashlib.sha256((artifact / PRIVATE_TOOL_BASENAME).read_bytes()).hexdigest()
    (artifact / MANIFEST_BASENAME).write_text(
        json.dumps(
            {
                "commit": PINNED_COMMIT,
                "archive_sha256": PINNED_ARCHIVE_SHA256,
                "executable": PRIVATE_TOOL_BASENAME,
                "executable_sha256": digest,
                pins.XDOTOOL_BUILD_INPUTS_KEY: _TEST_XDOTOOL_BUILD_INPUTS,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (artifact / PRIVATE_TOOL_BASENAME).chmod(0o700)
    (artifact / ARCHIVE_BASENAME).chmod(0o600)
    (artifact / MANIFEST_BASENAME).chmod(0o600)
    return artifact


def test_manifest_and_exclusive_template_match(tmp_path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    matcher = operator.TemplateMatcher(
        manifest,
        artifact_root=matcher_runtime.default_artifact_root(),
    )
    assert matcher.source_bindings["template-matcher-binary"] == matcher_runtime.EXPECTED_ARTIFACT_SHA256
    for source_name, source_digest in matcher_runtime.EXPECTED_SOURCE_HASHES.items():
        assert matcher.source_bindings[f"template-matcher-{source_name}"] == source_digest
    region = operator.NativeCalibrationOperator._control_region(Identity())
    deadline = time.monotonic() + 10.0
    try:
        match = matcher.require_exclusive(
            construct_scene,
            "construct",
            region=region,
            deadline_monotonic=deadline,
        )
        assert match.accepted
        assert match.click == (1144, 24)
        dual = Image.new("RGB", (1280, 720), (0, 0, 0))
        dual.paste(Image.open(construct_scene).crop((1142, 22, 1146, 26)), (1142, 22))
        dual.paste(Image.open(restore_scene).crop((1211, 22, 1215, 26)), (1211, 22))
        dual_path = tmp_path / "scene-dual-visible.png"
        dual.save(dual_path, format="PNG")
        assert matcher.require_exclusive(
            dual_path,
            "construct",
            region=region,
            deadline_monotonic=deadline,
        ).accepted
        assert matcher.require_exclusive(
            dual_path,
            "restore",
            region=region,
            deadline_monotonic=deadline,
        ).accepted
        with pytest.raises(operator.OperatorError, match="distinct"):
            operator.TemplateManifest(
                manifest.construct,
                operator.TemplateRole(manifest.construct.path, manifest.construct.sha256, 5.0),
            ).verify()
    finally:
        matcher.close()


def test_template_matcher_forwards_one_absolute_deadline_to_search_and_score(tmp_path: Path) -> None:
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    root = matcher_runtime.default_artifact_root()
    calls: list[tuple[str, float]] = []

    class RecordingRuntime:
        def __init__(self) -> None:
            self.delegate = matcher_runtime.TemplateMatcherRuntime(root)

        @property
        def source_bindings(self) -> dict[str, str]:
            return self.delegate.source_bindings

        def search(self, *args: Any, deadline_monotonic: float, **kwargs: Any):
            calls.append(("search", deadline_monotonic))
            return self.delegate.search(*args, deadline_monotonic=deadline_monotonic, **kwargs)

        def score_at(self, *args: Any, deadline_monotonic: float, **kwargs: Any):
            calls.append(("score_at", deadline_monotonic))
            return self.delegate.score_at(*args, deadline_monotonic=deadline_monotonic, **kwargs)

        def close(self) -> None:
            self.delegate.close()

    screen = FakeScreen(tmp_path, construct_scene, restore_scene)
    screen.last_point = (640, 360, 1)
    screen_deadlines: list[tuple[str, float | None]] = []
    original_capture = screen.capture
    original_resolve = screen.resolve_identity

    def recording_capture(path: Path, *, identity: Identity, deadline_monotonic: float | None = None):
        screen_deadlines.append(("capture", deadline_monotonic))
        return original_capture(path, identity=identity, deadline_monotonic=deadline_monotonic)

    def recording_resolve(*, require_foreground: bool = True, deadline_monotonic: float | None = None):
        screen_deadlines.append(("resolve", deadline_monotonic))
        return original_resolve(
            require_foreground=require_foreground,
            deadline_monotonic=deadline_monotonic,
        )

    screen.capture = recording_capture
    screen.resolve_identity = recording_resolve
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    candidate = object.__new__(operator.NativeCalibrationOperator)
    candidate.clock = time.monotonic
    candidate.receipt = SimpleNamespace(attempt=attempt, event=lambda *_args, **_kwargs: None)
    candidate.screen = screen
    candidate._identity_guard = lambda *_args, **_kwargs: screen.identity
    candidate._deadline = lambda *_args, **_kwargs: deadline

    recording = RecordingRuntime()
    matcher = operator.TemplateMatcher(manifest, runtime=recording)
    deadline = time.monotonic() + 20.0
    try:
        candidate.matcher = matcher
        assert candidate._capture("construct-control", require_exclusive="construct")[2].accepted
        assert calls == [("search", deadline), ("score_at", deadline)]
        assert screen_deadlines == [("capture", deadline), ("resolve", deadline)]

        calls.clear()
        screen_deadlines.clear()
        dual = Image.new("RGB", (1280, 720), (0, 0, 0))
        with Image.open(construct_scene) as construct, Image.open(restore_scene) as restore:
            dual.paste(construct.crop((1142, 22, 1146, 26)), (1142, 22))
            dual.paste(restore.crop((1211, 22, 1215, 26)), (1211, 22))
        dual_path = tmp_path / "dual-deadline.png"
        dual.save(dual_path, format="PNG")
        screen.dual = dual_path
        assert candidate._capture("post-reload-controls", require_dual=True)[2].accepted
        assert calls == [("search", deadline), ("search", deadline)]
        assert screen_deadlines == [("capture", deadline), ("resolve", deadline)]
    finally:
        matcher.close()


@pytest.mark.parametrize("failure", ["runtime", "timeout", "rejected"])
def test_click_matcher_failure_refuses_before_fixed_input_or_action_log(
    tmp_path: Path,
    failure: str,
) -> None:
    manifest, construct_scene, _restore_scene = make_scenes(tmp_path)
    delegate = matcher_runtime.TemplateMatcherRuntime(matcher_runtime.default_artifact_root())

    class FailingRuntime:
        @property
        def source_bindings(self) -> dict[str, str]:
            return delegate.source_bindings

        def search(self, *args: Any, deadline_monotonic: float, **kwargs: Any):
            result = delegate.search(*args, deadline_monotonic=deadline_monotonic, **kwargs)
            if failure == "runtime":
                raise matcher_runtime.MatcherError("recorded matcher failure")
            if failure == "timeout":
                raise matcher_runtime.MatcherTimeout("recorded matcher timeout")
            return matcher_runtime.MatcherResult(
                "rejected",
                result.count * 6,
                result.count,
                result.box,
            )

        def score_at(self, *args: Any, deadline_monotonic: float, **kwargs: Any):
            return delegate.score_at(*args, deadline_monotonic=deadline_monotonic, **kwargs)

        def close(self) -> None:
            delegate.close()

    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    receipt = SimpleNamespace(attempt=attempt, event=lambda *_args, **_kwargs: None)
    screen = FakeScreen(tmp_path, construct_scene, construct_scene)
    # This matcher failure regression is about dispatch after a rejection.  A
    # separately focused parking test covers the owned move path; keep this
    # screen already parked so no emitter is needed before matching.
    screen.last_point = (640, 360, 1)
    action_log = tmp_path / "actions.log"
    action_log.touch(mode=0o600)
    emitter_calls: list[str] = []

    class Tool:
        pass

    def forbidden_emitter() -> Any:
        emitter_calls.append("allocated")
        raise AssertionError("FixedInput allocated an emitter after matcher refusal")

    input_device = operator.FixedInput(
        Tool(),
        watchdog=FakeWatchdog(),
        run=lambda *_args, **_kwargs: action_log.write_text("dispatched", encoding="utf-8"),
        emitter_factory=forbidden_emitter,
    )
    matcher = operator.TemplateMatcher(manifest, runtime=FailingRuntime())
    candidate = object.__new__(operator.NativeCalibrationOperator)
    candidate.mode = operator.MODE_CONTROL_PREFLIGHT
    candidate._phase_deadlines = {}
    candidate._release_dispatch = None
    candidate.clock = time.monotonic
    candidate._recheck = lambda *_args, **_kwargs: {}
    candidate._identity_guard = lambda *_args, **_kwargs: screen.identity
    candidate._deadline = lambda *_args, **_kwargs: time.monotonic() + 20.0
    candidate.receipt = receipt
    candidate.screen = screen
    candidate.matcher = matcher
    candidate.input = input_device
    try:
        with pytest.raises(
            operator.OperatorError, match="native .*template matching failed|not role-exclusive"
        ):
            candidate._click("construct")
        assert emitter_calls == []
        assert action_log.read_text(encoding="utf-8") == ""
    finally:
        matcher.close()


@pytest.mark.parametrize("role", ["construct", "restore"])
def test_click_refuses_fresh_template_outside_source_button_before_input(tmp_path: Path, role: str) -> None:
    identity = Identity()
    source_rect = operator.NativeCalibrationOperator._source_control_rect(identity, role)
    assert source_rect is not None
    source_x, source_y, _source_width, _source_height = source_rect
    match = operator.TemplateMatch(
        role,
        True,
        0.0,
        (source_x - 1, source_y, 4, 4),
        (source_x + 1, source_y + 2),
        "c" * 64,
    )
    screen_calls: list[str] = []

    class Screen:
        def resolve_identity(self, *, require_foreground=True, deadline_monotonic=None):
            assert require_foreground
            return identity

        def pointer_position(self, *, deadline_monotonic=None):
            screen_calls.append("pointer")
            raise AssertionError("source geometry refusal must precede pointer access")

    class Input:
        def click(self, *_args, **_kwargs):
            raise AssertionError("source geometry refusal must precede click input")

    candidate = object.__new__(operator.NativeCalibrationOperator)
    candidate.mode = operator.MODE_CONTROL_PREFLIGHT
    candidate._phase_deadlines = {}
    candidate._release_dispatch = None
    candidate.generation = 7
    candidate.clock = time.monotonic
    candidate.receipt = SimpleNamespace(event=lambda *_args, **_kwargs: None)
    candidate.screen = Screen()
    candidate.input = Input()
    candidate._capture = lambda *_args, **_kwargs: (tmp_path / "fresh.png", identity, match)
    candidate._identity_guard = lambda *_args, **_kwargs: identity
    candidate._recheck = lambda *_args, **_kwargs: {}
    candidate._deadline = lambda *_args, **_kwargs: time.monotonic() + 20.0

    with pytest.raises(operator.OperatorError, match="outside its source-derived button"):
        candidate._click(role)
    assert screen_calls == []


def test_control_capture_parks_hovered_pointer_before_real_matcher(tmp_path: Path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    region = operator.NativeCalibrationOperator._control_region(Identity())
    assert region is not None
    hovered_scene = tmp_path / "restore-hovered.png"
    with Image.open(restore_scene) as image:
        image = image.convert("RGB")
        image.paste(Image.new("RGB", (4, 4), (255, 255, 255)), (1211, 22))
        image.save(hovered_scene, format="PNG")
    dual_scene = tmp_path / "dual-controls.png"
    dual_hovered_scene = tmp_path / "dual-controls-hovered.png"
    with Image.open(construct_scene) as construct, Image.open(restore_scene) as restore:
        dual = construct.convert("RGB")
        dual.paste(restore.crop((1211, 22, 1215, 26)), (1211, 22))
        dual.save(dual_scene, format="PNG")
        dual_hovered = dual.copy()
        dual_hovered.paste(Image.new("RGB", (4, 4), (255, 255, 255)), (1211, 22))
        dual_hovered.save(dual_hovered_scene, format="PNG")
    old_path = tmp_path / "old-hovered.png"
    shutil.copyfile(hovered_scene, old_path)
    matcher = operator.TemplateMatcher(
        manifest,
        runtime=matcher_runtime.TemplateMatcherRuntime(matcher_runtime.default_artifact_root()),
    )

    class HoverScreen(FakeScreen):
        def capture(self, path: Path, *, identity: Identity, deadline_monotonic=None):
            source = self.restore
            if "post-reload-controls" in path.name:
                source = dual_hovered_scene if self._contains_control(self.last_point, region) else dual_scene
            elif self._contains_control(self.last_point, region):
                source = hovered_scene
            path.write_bytes(source.read_bytes())
            os.chmod(path, 0o600)
            return self.identity

        @staticmethod
        def _contains_control(point, control_region):
            left, top, width, height = control_region
            return left <= point[0] < left + width and top <= point[1] < top + height

    screen = HoverScreen(tmp_path, construct_scene, restore_scene)
    screen.last_point = (1212, 23, 1)
    input_device = FakeInput(screen)
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    events: list[tuple[str, object]] = []
    candidate = object.__new__(operator.NativeCalibrationOperator)
    candidate.clock = time.monotonic
    candidate.receipt = SimpleNamespace(
        attempt=attempt, event=lambda name, **fields: events.append((name, fields))
    )
    candidate.screen = screen
    candidate.input = input_device
    candidate.matcher = matcher
    candidate._identity_guard = lambda *_args, **_kwargs: screen.identity
    candidate._deadline = lambda *_args, **_kwargs: time.monotonic() + 20.0
    try:
        with pytest.raises(
            operator.OperatorError, match="(?:native .*template matching failed|not role-exclusive)"
        ):
            matcher.require_exclusive(
                old_path,
                "restore",
                region=region,
                deadline_monotonic=time.monotonic() + 20.0,
            )
        dual_path, identity, dual_match = candidate._capture("post-reload-controls", require_dual=True)
        assert dual_match is not None and dual_match.accepted
        parked = screen.last_point[:2]
        assert not HoverScreen._contains_control(parked, region)
        assert not HoverScreen._contains_control(parked, screen.agent_crop)
        screen.last_point = (1212, 23, 1)
        path, identity, match = candidate._capture("restore-control", require_exclusive="restore")
        assert identity is screen.identity
        assert match is not None and match.accepted
        parked = screen.last_point[:2]
        assert not HoverScreen._contains_control(parked, region)
        assert not HoverScreen._contains_control(parked, screen.agent_crop)
        assert len([point for kind, point in input_device.calls if kind == "move"]) == 2
        assert [name for name, _fields in events] == [
            "pointer_parked",
            "capture",
            "pointer_parked",
            "capture",
        ]
        assert path.read_bytes() == restore_scene.read_bytes()
    finally:
        matcher.close()


def test_pointer_parking_is_noop_when_safe_and_refuses_unknown_canvas(tmp_path: Path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, construct_scene, restore_scene)
    screen.last_point = (640, 360, 1)
    input_device = FakeInput(screen)
    candidate = object.__new__(operator.NativeCalibrationOperator)
    candidate.screen = screen
    candidate.input = input_device
    candidate.clock = time.monotonic
    candidate._binding = None
    candidate._identity_guard = lambda *_args, **_kwargs: screen.identity
    candidate._deadline = lambda *_args, **_kwargs: time.monotonic() + 20.0
    assert (
        candidate._park_pointer(screen.identity, deadline_monotonic=time.monotonic() + 20.0)
        is screen.identity
    )
    assert input_device.calls == []
    screen.agent_crop = (0, 0, 0, 64)
    with pytest.raises(operator.OperatorError, match="canvas"):
        candidate._park_pointer(screen.identity, deadline_monotonic=time.monotonic() + 20.0)


@pytest.mark.parametrize(
    ("reason", "configure"),
    [
        (
            "generation",
            lambda candidate: setattr(
                candidate,
                "_recheck",
                lambda: (_ for _ in ()).throw(operator.OperatorError("generation changed")),
            ),
        ),
        ("deadline", lambda candidate: None),
    ],
)
def test_pointer_parking_refuses_stale_generation_or_expired_deadline_before_input(
    tmp_path: Path, reason: str, configure
):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, construct_scene, restore_scene)
    screen.last_point = (1212, 23, 1)
    input_device = FakeInput(screen)
    candidate = object.__new__(operator.NativeCalibrationOperator)
    candidate.screen = screen
    candidate.input = input_device
    candidate.clock = time.monotonic
    candidate._binding = object()
    candidate._identity_guard = lambda *_args, **_kwargs: screen.identity
    candidate._deadline = lambda *_args, **_kwargs: time.monotonic() + 20.0
    configure(candidate)
    deadline = time.monotonic() + 20.0 if reason == "generation" else time.monotonic() - 1.0
    with pytest.raises(operator.OperatorError, match="generation|expired"):
        candidate._park_pointer(screen.identity, deadline_monotonic=deadline)
    assert input_device.calls == []


def test_pointer_parking_rejects_endpoint_target_drift_after_owned_move(tmp_path: Path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, construct_scene, restore_scene)
    screen.last_point = (1212, 23, 1)

    class WrongTargetInput(FakeInput):
        def move_pointer(self, gesture, *, identity, before_press=None, deadline_monotonic=None):
            if before_press is not None:
                before_press()
            left, top, _width, _height = identity.child_geometry
            point = (left + int(gesture.endpoint.x), top + int(gesture.endpoint.y))
            self.calls.append(("move", point))
            self.screen.last_point = (*point, 999)

    input_device = WrongTargetInput(screen)
    candidate = object.__new__(operator.NativeCalibrationOperator)
    candidate.screen = screen
    candidate.input = input_device
    candidate.clock = time.monotonic
    candidate._binding = None
    candidate._identity_guard = lambda *_args, **_kwargs: screen.identity
    with pytest.raises(operator.OperatorError, match="target"):
        candidate._park_pointer(screen.identity, deadline_monotonic=time.monotonic() + 20.0)
    assert len(input_device.calls) == 1 and input_device.calls[0][0] == "move"


def test_fixed_input_move_pointer_uses_owned_watchdog_without_button(tmp_path: Path):
    class Tool:
        proc_path = "/proc/self/fd/5"

        class Image:
            fd = 5

        image = Image()

    watchdog = FakeWatchdog()
    events: list[str] = []

    class Emitter:
        pgid = 94

        def start(self):
            events.append("start")

        def press(self):
            events.append("press")

        def release(self):
            events.append("release")

        def abort(self):
            events.append("abort")

    input_device = operator.FixedInput(Tool(), watchdog=watchdog, emitter_factory=lambda: Emitter())
    identity = Identity()
    input_device.move_pointer(_movement_gesture(identity), identity=identity)
    assert events == ["start", "press", "release"]
    assert watchdog.states == [
        {"groups": {94}, "buttons": set(), "keys": set()},
        {"groups": set(), "buttons": set(), "keys": set()},
    ]


def test_fixed_input_move_pointer_failure_cleans_owned_child_and_watchdog():
    class Tool:
        proc_path = "/proc/self/fd/5"

        class Image:
            fd = 5

        image = Image()

    watchdog = FakeWatchdog()
    events: list[str] = []

    class Emitter:
        pgid = 95

        def start(self):
            events.append("start")

        def press(self):
            events.append("press")
            raise operator.OperatorError("move dispatch failed")

        def release(self):
            events.append("release")

        def abort(self):
            events.append("abort")

    input_device = operator.FixedInput(Tool(), watchdog=watchdog, emitter_factory=lambda: Emitter())
    with pytest.raises(operator.OperatorError, match="move dispatch failed"):
        identity = Identity()
        input_device.move_pointer(_movement_gesture(identity), identity=identity)
    assert events == ["start", "press", "abort"]
    assert watchdog.states[-1] == {"groups": set(), "buttons": set(), "keys": set()}


def test_fixed_input_move_pointer_real_owned_process_has_watchdog_before_go(tmp_path: Path):
    artifact = make_gated_recording_artifact(tmp_path)
    action_log = tmp_path / "owned-actions.log"
    action_ack = tmp_path / "owned-action-acks"
    action_ack.mkdir(mode=0o700)
    environment = {
        "CROWLEY_ACTION_LOG": str(action_log),
        "CROWLEY_ACTION_ACK_DIR": str(action_ack),
    }
    tool = operator.PinnedInputTool.from_root(artifact)
    watchdog = operator.Watchdog(tool, interval=0.02, environment=environment)
    input_device = operator.FixedInput(tool, watchdog=watchdog, environment=environment)
    created: list[operator.OwnedEmitter] = []
    ownership_before_go: list[dict[str, int | bool]] = []
    original_new_emitter = input_device._new_emitter
    original_publish = watchdog.publish
    stop = threading.Event()
    ack_errors: list[BaseException] = []

    def record_emitter(action, *, release_action=(), deadline_monotonic=None, ready_timeout=2.0):
        emitter = original_new_emitter(
            action,
            release_action=release_action,
            deadline_monotonic=deadline_monotonic,
            ready_timeout=ready_timeout,
        )
        created.append(emitter)
        return emitter

    def prove_watchdog_ownership(*, groups, buttons, keys=frozenset(), deadline_monotonic=None):
        assert created
        process = created[-1]._process
        assert process is not None and process.pid is not None and process.is_alive()
        ownership_before_go.append(
            {"emitter_pid": process.pid, "emitter_alive": process.is_alive(), "pgid": next(iter(groups))}
        )
        return original_publish(
            groups=groups,
            buttons=buttons,
            keys=keys,
            deadline_monotonic=deadline_monotonic,
        )

    input_device._new_emitter = record_emitter  # type: ignore[method-assign]
    watchdog.publish = prove_watchdog_ownership  # type: ignore[method-assign]

    def acknowledge_actions() -> None:
        pending = b""
        offset = 0
        try:
            while not stop.is_set():
                if action_log.exists():
                    with action_log.open("rb") as stream:
                        stream.seek(offset)
                        chunk = stream.read(4096)
                        offset = stream.tell()
                    if chunk:
                        pending = _consume_action_records(
                            pending,
                            chunk,
                            on_record=lambda pid, _arguments: (action_ack / str(pid)).touch(
                                mode=0o600, exist_ok=True
                            ),
                        )
                time.sleep(0.002)
        except BaseException as exc:
            ack_errors.append(exc)

    acknowledger = threading.Thread(target=acknowledge_actions, daemon=True)
    acknowledger.start()
    try:
        identity = Identity()
        input_device.move_pointer(_movement_gesture(identity), identity=identity)
        assert ack_errors == []
        assert len(ownership_before_go) == 1
        assert ownership_before_go[0]["emitter_alive"] is True
        assert created[0]._process is not None and not created[0]._process.is_alive()
        assert action_log.read_text(encoding="utf-8").splitlines()
        action_rows = [line.split("\t")[2:] for line in action_log.read_text().splitlines()]
        assert len(action_rows) == 1
        assert action_rows[0][0] == "sleep"
        assert action_rows[0].count("mousemove") >= 2
        assert "--sync" not in action_rows[0]
        assert all(row[0] not in {"mousedown", "mouseup", "keydown", "keyup"} for row in action_rows)
    finally:
        stop.set()
        acknowledger.join(timeout=2.0)
        input_device.close()
        assert not created or created[0]._process is None or not created[0]._process.is_alive()
        tool.close()


def test_fixed_input_move_pointer_real_owned_failure_reaps_after_dispatch(tmp_path: Path, monkeypatch):
    artifact = make_recording_artifact(tmp_path)
    action_log = tmp_path / "owned-actions.log"
    environment = {"CROWLEY_RECORDER": str(action_log)}
    tool = operator.PinnedInputTool.from_root(artifact)
    watchdog = operator.Watchdog(tool, interval=0.02, environment=environment)
    input_device = operator.FixedInput(tool, watchdog=watchdog, environment=environment)
    created: list[operator.OwnedEmitter] = []
    original_new_emitter = input_device._new_emitter

    def record_emitter(action, *, release_action=(), deadline_monotonic=None, ready_timeout=2.0):
        emitter = original_new_emitter(
            action,
            release_action=release_action,
            deadline_monotonic=deadline_monotonic,
            ready_timeout=ready_timeout,
        )
        created.append(emitter)
        return emitter

    input_device._new_emitter = record_emitter  # type: ignore[method-assign]
    original_press = operator.OwnedEmitter.press

    def fail_after_real_dispatch(emitter):
        original_press(emitter)
        raise operator.OperatorError("test failure after owned pointer dispatch")

    monkeypatch.setattr(operator.OwnedEmitter, "press", fail_after_real_dispatch)
    try:
        with pytest.raises(operator.OperatorError, match="after owned pointer dispatch"):
            identity = Identity()
            input_device.move_pointer(_movement_gesture(identity), identity=identity)
        assert created and created[0]._process is not None and not created[0]._process.is_alive()
        action_text = action_log.read_text(encoding="utf-8").splitlines()
        assert len(action_text) == 1 and action_text[0].startswith("sleep ")
        assert not any(
            token in action_text[0].split() for token in {"mousedown", "mouseup", "keydown", "keyup"}
        )
        assert input_device.cleanup_failures == []
    finally:
        input_device.close()
        tool.close()


@pytest.mark.parametrize("final_failure", ["reservation", "geometry", "deadline"])
def test_pointer_parking_rechecks_admission_after_watchdog_before_owned_go(
    tmp_path: Path, final_failure: str
):
    """A revoked final admission must stop the real emitter before its GO."""

    artifact = make_gated_recording_artifact(tmp_path)
    action_log = tmp_path / "owned-actions.log"
    action_ack = tmp_path / "owned-action-acks"
    action_ack.mkdir(mode=0o700)
    environment = {
        "CROWLEY_ACTION_LOG": str(action_log),
        "CROWLEY_ACTION_ACK_DIR": str(action_ack),
    }
    tool = operator.PinnedInputTool.from_root(artifact)
    watchdog = operator.Watchdog(tool, interval=0.02, environment=environment)
    input_device = operator.FixedInput(tool, watchdog=watchdog, environment=environment)
    created: list[operator.OwnedEmitter] = []
    original_new_emitter = input_device._new_emitter
    original_publish = watchdog.publish
    original_clear = watchdog.clear
    published = threading.Event()
    clear_calls: list[bool] = []

    def record_emitter(action, *, release_action=(), deadline_monotonic=None, ready_timeout=2.0):
        emitter = original_new_emitter(
            action,
            release_action=release_action,
            deadline_monotonic=deadline_monotonic,
            ready_timeout=ready_timeout,
        )
        created.append(emitter)
        return emitter

    def publish_after_ownership(*, groups, buttons, keys=frozenset(), deadline_monotonic=None):
        result = original_publish(
            groups=groups,
            buttons=buttons,
            keys=keys,
            deadline_monotonic=deadline_monotonic,
        )
        published.set()
        if final_failure == "deadline":
            assert release_publish.wait(5.0)
        return result

    def record_clear():
        clear_calls.append(True)
        return original_clear()

    input_device._new_emitter = record_emitter  # type: ignore[method-assign]
    watchdog.publish = publish_after_ownership  # type: ignore[method-assign]
    watchdog.clear = record_clear  # type: ignore[method-assign]

    screen = FakeScreen(tmp_path, tmp_path / "unused-construct.png", tmp_path / "unused-restore.png")
    screen.last_point = (1212, 23, 1)
    capture_calls: list[bool] = []

    def unexpected_capture(*_args, **_kwargs):
        capture_calls.append(True)
        raise AssertionError("capture must not run after final pointer admission refusal")

    screen.capture = unexpected_capture  # type: ignore[method-assign]
    matcher_calls: list[bool] = []

    class UnexpectedMatcher:
        def require_exclusive(self, *_args, **_kwargs):
            matcher_calls.append(True)
            raise AssertionError("matcher must not run after final pointer admission refusal")

    candidate = object.__new__(operator.NativeCalibrationOperator)
    candidate.screen = screen
    candidate.input = input_device
    candidate.matcher = UnexpectedMatcher()
    candidate.clock = time.monotonic
    candidate._binding = object()
    candidate._event = lambda *_args, **_kwargs: None
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    candidate.receipt = SimpleNamespace(attempt=attempt, event=lambda *_args, **_kwargs: None)
    candidate._last_chat_capture_record = None
    candidate._chat_capture_sequence = 0
    deadline = time.monotonic() + 20.0
    release_after_deadline: threading.Thread | None = None
    release_publish = threading.Event()
    if final_failure == "deadline":
        deadline = time.monotonic() + 3.0

        def expire_publish_barrier() -> None:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            release_publish.set()

        release_after_deadline = threading.Thread(target=expire_publish_barrier, daemon=True)
        release_after_deadline.start()
    recheck_calls = 0
    guard_calls = 0

    def recheck(expected_state=None, *, deadline_monotonic=None):
        nonlocal recheck_calls
        recheck_calls += 1
        if recheck_calls == 2:
            assert published.is_set()
            if final_failure == "reservation":
                raise operator.OperatorError("native reservation was revoked before pointer dispatch")
        return {}

    def identity_guard(deadline_monotonic=None):
        nonlocal guard_calls
        guard_calls += 1
        assert deadline_monotonic == deadline
        if guard_calls == 3:
            assert published.is_set()
            if final_failure == "geometry":
                return replace(screen.identity, child_geometry=(1, 1, 1280, 720))
        return screen.identity

    candidate._recheck = recheck
    candidate._identity_guard = identity_guard

    try:
        with pytest.raises(operator.OperatorError, match="(?:revoked|geometry|deadline)"):
            candidate._capture(
                "restore-control",
                require_exclusive="restore",
                deadline_monotonic=deadline,
            )
        assert published.is_set()
        assert recheck_calls == 2
        assert guard_calls == (3 if final_failure != "reservation" else 2)
        assert clear_calls == [True]
        assert capture_calls == []
        assert matcher_calls == []
        assert not action_log.exists() or action_log.read_text(encoding="utf-8") == ""
        assert created and created[0]._process is not None and not created[0]._process.is_alive()
        assert input_device.cleanup_failures == []
    finally:
        if release_after_deadline is not None:
            release_after_deadline.join(timeout=1.0)
        input_device.close()
        tool.close()


def test_reservation_binding_rejects_boot_substitution():
    record = FakeBinding().record(7)
    record["expectedConstructorBoot"] = "boot-1"
    with pytest.raises(operator.OperatorError, match="expected constructor boot"):
        operator.ReservationBinding.from_record(record, expected_generation=7, now=1.0)


def test_reservation_binding_allows_zero_initial_guardian_generation():
    binding = operator.ReservationBinding.from_record(FakeBinding().record(7), expected_generation=7, now=1.0)
    assert binding.guardian_generation == 0
    assert binding.expected_constructor_boot == "native-construction:boot-1"


def test_daemon_client_uses_exact_closed_request_and_no_retry(tmp_path):
    calls = []

    def request(path, request, *, timeout):
        calls.append((path, request, timeout))
        return {
            "ok": True,
            "reservation": FakeBinding().record(7),
            "handshake": {"state": "construction_prepared"},
        }

    client = operator.DaemonClient(tmp_path / "session.sock", request_fn=request, clock=lambda: 1.0)
    client.prepare(7)
    client.recheck(7, "reservation-1", deadline=2.0)
    client.native_calibrate(7, deadline=2.0)
    client.calibration_status(deadline=2.0)
    client.status(deadline=2.0)
    client.pause(7, deadline=2.0)
    assert calls[0][1] == {"command": "native_construction_prepare", "generation": 7}
    with pytest.raises(operator.OperatorError):
        client.request({"command": "native_construction_prepare", "generation": 7, "extra": True})
    assert calls[1][1] == {
        "command": "native_construction_recheck",
        "generation": 7,
        "reservationId": "reservation-1",
    }
    assert calls[2][1] == {"command": "native_calibrate", "generation": 7}
    assert calls[3][1] == {"command": "calibration_status"}
    assert calls[4][1] == {"command": "status"}
    assert calls[5][1] == {
        "command": "pause",
        "generation": 7,
        "reason": "native construction recovery",
    }
    assert len(calls) == 6


def test_receipt_is_private_atomic_and_rejects_forbidden_fields(tmp_path):
    writer = operator.ReceiptWriter(tmp_path / "root", operator.MODE_CONTROL_PREFLIGHT)
    writer.begin()
    writer.event("safe", generation=7)
    with pytest.raises(operator.ReceiptError):
        writer.event("bad", path="/secret")
    writer.finish("refused", "visual_evidence_refused")
    receipt = writer.attempt / "receipt.json"
    assert receipt.stat().st_mode & 0o077 == 0
    loaded = json.loads(receipt.read_text())
    assert loaded["outcome"] == "refused"
    assert not (writer.attempt / ".receipt.json.tmp").exists()


def test_fixed_input_publishes_before_press_and_clears_after_release(tmp_path: Path, monkeypatch):
    rate_directory = tmp_path / "rate-state"
    rate_directory.mkdir(mode=0o700)
    monkeypatch.setattr(pointer_execution, "_session_state_directory", lambda: rate_directory)

    class Tool:
        proc_path = "/proc/self/fd/5"

        class Image:
            fd = 5

        image = Image()

    calls = []
    watchdog = FakeWatchdog()
    emitter_events = []

    class Emitter:
        pgid = 77

        def start(self):
            emitter_events.append("start")

        def press(self):
            emitter_events.append("press")

        def release(self):
            emitter_events.append("release")

        def abort(self):
            emitter_events.append("abort")

    def run(argv, **kwargs):
        calls.append(tuple(argv[1:]))

    input_device = operator.FixedInput(Tool(), watchdog=watchdog, run=run, emitter_factory=lambda: Emitter())
    identity = Identity()
    match = operator.TemplateMatch("construct", True, 0.0, (5, 5, 4, 4), (7, 7), "c" * 64)
    gesture = _click_gesture(match, identity)
    result = input_device.click(match, identity=identity, gesture=gesture)
    assert calls == []
    assert emitter_events == ["start", "press", "release", "start", "press", "release"]
    assert result["motionCompletedMonotonicNs"] >= result["motionStartedMonotonicNs"]
    assert result["clickReleaseAckMonotonicNs"] >= result["clickActionCompletedMonotonicNs"]
    assert watchdog.states == [
        {"groups": {77}, "buttons": set(), "keys": set()},
        {"groups": set(), "buttons": set(), "keys": set()},
        {"groups": {77}, "buttons": {1}, "keys": set()},
        {"groups": set(), "buttons": set(), "keys": set()},
    ]


def test_fixed_input_real_owned_click_uses_gesture_gate_and_exact_button_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact = make_gated_recording_artifact(tmp_path)
    action_log = tmp_path / "owned-click-actions.log"
    action_ack = tmp_path / "owned-click-action-acks"
    action_ack.mkdir(mode=0o700)
    environment = {
        "CROWLEY_ACTION_LOG": str(action_log),
        "CROWLEY_ACTION_ACK_DIR": str(action_ack),
    }
    rate_directory = tmp_path / "rate-state"
    rate_directory.mkdir(mode=0o700)
    monkeypatch.setattr(pointer_execution, "_session_state_directory", lambda: rate_directory)
    tool = operator.PinnedInputTool.from_root(artifact)
    watchdog = operator.Watchdog(tool, interval=0.02, environment=environment)
    input_device = operator.FixedInput(tool, watchdog=watchdog, environment=environment)
    stop = threading.Event()
    errors: list[BaseException] = []

    def acknowledge() -> None:
        pending = b""
        offset = 0
        try:
            while not stop.is_set():
                if action_log.exists():
                    with action_log.open("rb") as stream:
                        stream.seek(offset)
                        chunk = stream.read(4096)
                        offset = stream.tell()
                    if chunk:
                        pending = _consume_action_records(
                            pending,
                            chunk,
                            on_record=lambda pid, _arguments: (action_ack / str(pid)).touch(
                                mode=0o600, exist_ok=True
                            ),
                        )
                time.sleep(0.002)
        except BaseException as exc:
            errors.append(exc)

    acknowledger = threading.Thread(target=acknowledge, daemon=True)
    acknowledger.start()
    identity = Identity()
    match = operator.TemplateMatch("construct", True, 0.0, (1142, 22, 4, 4), (1144, 24), "d" * 64)
    gesture = _click_gesture(match, identity, seed=31)
    try:
        result = input_device.click(match, identity=identity, gesture=gesture)
        assert errors == []
        assert result["clickPrecommitMonotonicNs"] > 0
        assert result["rateRecordMonotonicNs"] >= result["clickActionCompletedMonotonicNs"]
        rows = [line.split("\t")[2:] for line in action_log.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 2
        assert rows[0][0] == "sleep" and rows[0].count("mousemove") >= 2
        assert "--sync" not in rows[0]
        assert rows[1][:3] == ["mousedown", "1", "sleep"]
        assert rows[1][-2:] == ["mouseup", "1"]
        state = pointer_execution._read_state(rate_directory)
        assert state is not None
        assert state["lastPressMonotonicNs"] == result["rateRecordMonotonicNs"]
        assert state["nextAllowedMonotonicNs"] == result["rateRecordMonotonicNs"] + 1_000_000_000
    finally:
        stop.set()
        acknowledger.join(timeout=2.0)
        input_device.close()
        tool.close()


def test_fixed_input_type_publishes_exact_nonempty_keysyms_and_long_budget():
    class Tool:
        proc_path = "/proc/self/fd/5"

        class Image:
            fd = 5

        image = Image()

    watchdog = FakeWatchdog()
    events = []

    class Emitter:
        pgid = 91

        def start(self):
            events.append("start")

        def press(self):
            events.append("press")

        def release(self):
            events.append("release")

        def abort(self):
            events.append("abort")

    command = "/crowley agent finalize"
    input_device = operator.FixedInput(Tool(), watchdog=watchdog, emitter_factory=lambda: Emitter())
    input_device.type_command(command)
    assert events == ["start", "press", "release"]
    assert watchdog.states[0]["keys"] == operator._text_keysyms(command)
    assert watchdog.states[0]["keys"]
    assert operator._action_timeout(("type", "--delay", "130", command)) > 2.0
    with pytest.raises(operator.OperatorError, match="allowlisted"):
        operator._action_timeout(("type", "--delay", "130", "/not-fixed"))


def test_pointer_click_timeout_includes_bounded_owned_ack_overhead():
    dwell = 0.123456
    action = ("mousedown", "1", "sleep", f"{dwell:.6f}", "mouseup", "1")
    assert operator._action_timeout(action) == pytest.approx(
        dwell + operator._POINTER_CLICK_ACK_RESERVE_SECONDS
    )
    with pytest.raises(operator.OperatorError, match="cannot fit"):
        operator._action_timeout(action, deadline=time.monotonic() + 0.01)


def test_click_complete_deadline_refuses_before_pointer_motion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    rate_directory = tmp_path / "rate-state"
    rate_directory.mkdir(mode=0o700)
    monkeypatch.setattr(pointer_execution, "_session_state_directory", lambda: rate_directory)

    class Tool:
        proc_path = "/proc/self/fd/5"

        class Image:
            fd = 5

        image = Image()

    events: list[str] = []

    class Emitter:
        pgid = 94

        def start(self):
            events.append("start")

        def press(self):
            events.append("press")

        def release(self):
            events.append("release")

        def abort(self):
            events.append("abort")

    watchdog = FakeWatchdog()
    input_device = operator.FixedInput(Tool(), watchdog=watchdog, emitter_factory=lambda: Emitter())
    identity = Identity()
    match = operator.TemplateMatch("construct", True, 0.0, (5, 5, 4, 4), (7, 7), "c" * 64)
    gesture = _click_gesture(match, identity, seed=53)
    with pointer_execution.acquire_click_rate_permit() as permit:
        with pytest.raises(operator.OperatorError, match="complete click gesture cannot fit"):
            input_device.click(
                match,
                identity=identity,
                gesture=gesture,
                rate_permit=permit,
                deadline_monotonic=time.monotonic() + 1.0,
            )
    assert events == []
    assert watchdog.states == []


def test_fixed_input_refusal_before_go_clears_ownership_without_release(tmp_path: Path, monkeypatch):
    rate_directory = tmp_path / "rate-state"
    rate_directory.mkdir(mode=0o700)
    monkeypatch.setattr(pointer_execution, "_session_state_directory", lambda: rate_directory)

    class Tool:
        proc_path = "/proc/self/fd/5"

        class Image:
            fd = 5

        image = Image()

    watchdog = FakeWatchdog()
    events = []

    class Emitter:
        pgid = 92

        def start(self):
            events.append("start")

        def press(self):
            events.append("press")

        def release(self):
            events.append("release")

        def abort(self):
            events.append("abort")

    input_device = operator.FixedInput(Tool(), watchdog=watchdog, emitter_factory=lambda: Emitter())
    match = operator.TemplateMatch("construct", True, 0.0, (5, 5, 4, 4), (7, 7), "c" * 64)
    identity = Identity()
    gesture = _click_gesture(match, identity)

    def refuse_before_press():
        raise operator.OperatorError("identity drift")

    with pytest.raises(operator.OperatorError, match="identity drift"):
        input_device.click(match, identity=identity, gesture=gesture, before_press=refuse_before_press)
    assert events == ["start", "press", "release", "start", "abort"]
    assert watchdog.states[-1] == {"groups": set(), "buttons": set(), "keys": set()}


def test_fixed_input_phase_budget_refuses_before_go():
    class Tool:
        proc_path = "/proc/self/fd/5"

        class Image:
            fd = 5

        image = Image()

    watchdog = FakeWatchdog()
    events = []

    class Emitter:
        pgid = 93

        def start(self):
            events.append("start")

        def press(self):
            events.append("press")

        def release(self):
            events.append("release")

        def abort(self):
            events.append("abort")

    input_device = operator.FixedInput(Tool(), watchdog=watchdog, emitter_factory=lambda: Emitter())
    with pytest.raises(operator.OperatorError, match="cannot fit"):
        input_device.type_command("/crowley agent finalize", deadline_monotonic=time.monotonic() + 0.001)
    assert events == ["start", "abort"]
    assert watchdog.states[-1] == {"groups": set(), "buttons": set(), "keys": set()}


def test_action_budget_refuses_before_physical_dispatch_and_deadline_is_monotone():
    action = ("type", "--delay", "130", "/crowley agent finalize")
    with pytest.raises(operator.OperatorError, match="cannot fit"):
        operator._action_timeout(action, deadline=time.monotonic() + 0.001)

    candidate = object.__new__(operator.NativeCalibrationOperator)
    candidate._reservation_deadline = 10.0
    candidate._operation_deadline = 20.0
    candidate._phase_deadlines = {"constructor": 30.0}
    assert candidate._deadline("constructor") == 10.0
    candidate._reservation_deadline = None
    assert candidate._deadline("constructor") == 20.0


def test_record_operation_deadline_never_extends_an_observed_bound():
    candidate = object.__new__(operator.NativeCalibrationOperator)
    candidate._operation_deadline = None
    candidate._phase_deadlines = {}
    candidate._handshake = "running"

    candidate._record_operation_deadline(
        {
            "handshake": {
                "operationDeadline": 200.0,
                "constructorClickDeadline": 210.0,
                "providerDeadline": 250.0,
                "finalizeHookDeadline": 260.0,
                "finalizeDeadline": 270.0,
            }
        }
    )
    assert candidate._operation_deadline == 200.0
    assert candidate._phase_deadlines == {
        "constructor": 210.0,
        "provider": 250.0,
        "finalize_hook": 260.0,
        "finalize": 270.0,
    }
    candidate._record_operation_deadline(
        {
            "handshake": {
                "operationDeadline": 300.0,
                "constructorClickDeadline": 310.0,
                "providerDeadline": 350.0,
                "finalizeHookDeadline": 360.0,
                "finalizeDeadline": 370.0,
            }
        }
    )
    assert candidate._operation_deadline == 200.0
    assert candidate._phase_deadlines == {
        "constructor": 210.0,
        "provider": 250.0,
        "finalize_hook": 260.0,
        "finalize": 270.0,
    }
    candidate._record_operation_deadline(
        {
            "handshake": {
                "operationDeadline": 150.0,
                "constructorClickDeadline": 205.0,
                "providerDeadline": 175.0,
                "finalizeHookDeadline": 255.0,
                "finalizeDeadline": 265.0,
            }
        }
    )
    assert candidate._operation_deadline == 150.0
    assert candidate._phase_deadlines == {
        "constructor": 205.0,
        "provider": 175.0,
        "finalize_hook": 255.0,
        "finalize": 265.0,
    }

    # A later provider response may advertise a longer value, but the
    # constructor recheck/click path must still refuse after the original
    # constructor bound.  This is the release+10 bounded phase, not a
    # provider deadline that can widen it.
    candidate.mode = operator.MODE_MEASURE_DEFERRED
    candidate.clock = time.monotonic
    candidate._phase_deadlines["constructor"] = time.monotonic() - 1.0
    candidate._release_dispatch = time.monotonic() - 9.0
    with pytest.raises(operator.OperatorError, match="constructor click deadline expired"):
        candidate._click("construct")


def test_owned_emitter_invalid_process_group_reaps_child_and_closes_fds(tmp_path, monkeypatch):
    artifact = make_gated_recording_artifact(tmp_path)
    tool = operator.PinnedInputTool.from_root(artifact)
    emitter = operator.OwnedEmitter(tool, context=multiprocessing.get_context("fork"))
    monkeypatch.setattr(operator.os, "getpgid", lambda _pid: 0)
    try:
        with pytest.raises(operator.OperatorError, match="process group is malformed"):
            emitter.start()
        assert emitter._process is not None
        assert not emitter._process.is_alive()
        assert emitter._process.exitcode is not None
        assert emitter.pgid == 0
        assert all(
            getattr(emitter, descriptor) == -1
            for descriptor in ("_gate_read", "_gate_write", "_status_read", "_status_write")
        )
    finally:
        emitter.abort()
        tool.close()


def test_watchdog_missing_owner_start_refuses_before_child_allocation(tmp_path, monkeypatch):
    artifact = make_recording_artifact(tmp_path)
    tool = operator.PinnedInputTool.from_root(artifact)
    context_calls: list[str] = []

    def forbidden_context(name: str):
        context_calls.append(name)
        raise AssertionError("watchdog child context was allocated")

    monkeypatch.setattr(operator.multiprocessing, "get_context", forbidden_context)
    try:
        with pytest.raises(operator.OperatorError, match="owner start identity is unavailable"):
            operator.Watchdog(tool, owner_pid=os.getpid(), owner_start="")
        assert context_calls == []
    finally:
        tool.close()


def test_cancellation_child_closes_inherited_nonprotocol_fd_and_keeps_protocol(tmp_path, monkeypatch):
    socket_path = tmp_path / "cancellation-fd.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    sentinel_read, sentinel_write = os.pipe()
    fork_context = multiprocessing.get_context("fork")
    monkeypatch.setattr(operator.multiprocessing, "get_context", lambda _name: fork_context)
    actor = operator.CancellationActor(
        socket_path,
        generation=31,
        reservation_id="",
        deadline=time.monotonic() + 20.0,
        owner_pid=os.getpid(),
        owner_start=operator._proc_start(os.getpid()),
    )
    server_thread: threading.Thread | None = None
    received: list[dict] = []

    def serve_once() -> None:
        connection, _address = listener.accept()
        with connection:
            frame = bytearray()
            while not frame.endswith(b"\n"):
                chunk = connection.recv(4096)
                if not chunk:
                    raise AssertionError("cancellation actor closed protocol before request")
                frame.extend(chunk)
            received.append(json.loads(frame[:-1].decode("utf-8")))
            reply = _admission_reply(
                owner_pid=os.getpid(),
                owner_start=operator._proc_start(os.getpid()) or "server-start",
                generation=31,
                reservation_id="measure-31",
                kind="measure",
                state="prepared",
                handshake_state="construction_prepared",
                history=["construction_prepared"],
            )
            connection.sendall(json.dumps(reply, separators=(",", ":")).encode() + b"\n")

    try:
        actor.start()
        assert actor._process is not None
        actor_pid = actor._process.pid
        assert actor_pid is not None
        try:
            os.readlink(f"/proc/{actor_pid}/fd/{sentinel_read}")
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("cancellation child retained a nonprotocol descriptor")
        server_thread = threading.Thread(target=serve_once, daemon=True)
        server_thread.start()
        response = actor.call(
            "prepare",
            generation=31,
            preflight=False,
            deadline=time.monotonic() + 10.0,
        )
        assert response["reservation"]["reservationId"] == "measure-31"
        assert received == [{"command": "native_construction_prepare", "generation": 31}]
    finally:
        actor.close()
        if server_thread is not None:
            server_thread.join(timeout=3.0)
        listener.close()
        os.close(sentinel_read)
        os.close(sentinel_write)
    assert actor._process is not None and not actor._process.is_alive()


def test_production_reload_boundary_uses_profile_bound_strict_absence(tmp_path, monkeypatch):
    identity = Identity()
    screen = object.__new__(operator.X11Screen)
    screen.root = tmp_path
    screen.config = SimpleNamespace(
        profile=SimpleNamespace(canvas_crop=(0, 0, 4, 4), client_width=4, client_height=4)
    )
    calls = []

    def resolve_identity(*, require_foreground=True, deadline_monotonic=None):
        assert require_foreground
        return identity

    def capture(path, *, identity, deadline_monotonic=None):
        Image.new("RGB", (4, 4), (0, 0, 0)).save(path, format="PNG")
        return identity

    screen.resolve_identity = resolve_identity
    screen.capture = capture
    monkeypatch.setattr(
        operator,
        "_strict_native_absence_image",
        lambda path, *, expected_size: calls.append((path, expected_size)) or True,
    )
    result = screen.wait_reload_boundary(
        deadline_monotonic=time.monotonic() + 2.0,
        proof="absence",
    )
    assert result is identity
    assert calls and calls[0][1] == (4, 4)
    assert not list(tmp_path.iterdir())


def test_chat_readback_mismatch_escapes_without_submit(tmp_path):
    manifest, _construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, restore_scene, restore_scene)
    input_device = FakeInput(screen)
    client = FakeClient(7, ["construction_prepared", "construction_prepared"])
    op = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=client,
        screen=screen,
        input_device=input_device,
        ocr=lambda path: "" if "chat-closed" in path.name else "/wrong",
        receipt_root=tmp_path / "receipts",
    )
    op.receipt.begin()
    op._binding = operator.ReservationBinding.from_record(
        FakeBinding().record(7), expected_generation=7, now=1.0
    )
    op._reservation_deadline = 1000000000000.0
    op._handshake = "construction_prepared"
    with pytest.raises(operator.OperatorError, match="readback"):
        op._chat("/crowley agent defer", expected_state="construction_prepared")
    assert ("key", "Escape") in input_device.calls
    assert ("key", "Return") not in input_device.calls[2:]
    assert op.receipt.attempt is not None
    retained = sorted((op.receipt.attempt / "chat-fullframes").glob("*.png"))
    assert len(retained) == 2
    diagnostic = op.receipt.attempt / "chat-readback-00000002.json"
    assert diagnostic.stat().st_mode & 0o777 == 0o600
    report = json.loads(diagnostic.read_text(encoding="utf-8"))
    failed = report["failed_typed_capture"]
    assert failed["sequence"] == 2
    assert failed["use"] == "typed_chat_readback"
    assert failed["typed_editor_state"] == operator.CHAT_EDITOR_INACTIVE
    retained_typed = op.receipt.attempt / "chat-fullframes" / failed["filename"]
    assert retained_typed.read_bytes() == restore_scene.read_bytes()
    assert failed["original_sha256"] == hashlib.sha256(retained_typed.read_bytes()).hexdigest()
    typed_crop = op.receipt.attempt / failed["derived_crop_filename"]
    assert failed["derived_crop_sha256"] == hashlib.sha256(typed_crop.read_bytes()).hexdigest()
    assert report["observed_raw"] == "/wrong"
    assert report["observed_normalized"] == "/wrong"
    op.input.close()


def test_chat_fullframe_retention_is_bounded_and_repeated_labels_are_distinct(tmp_path):
    manifest, _construct_scene, restore_scene = make_scenes(tmp_path)
    source_paths = []
    with Image.open(restore_scene) as source_image:
        for index in range(5):
            source = source_image.copy()
            source.putpixel((index, 0), (20 + index, 30 + index, 40 + index))
            source_path = tmp_path / f"retention-source-{index}.png"
            source.save(source_path, format="PNG")
            source_paths.append(source_path)

    class RepeatedLabelScreen(FakeScreen):
        def __init__(self):
            super().__init__(tmp_path, restore_scene, restore_scene)
            self.source_index = 0

        def capture(self, path, *, identity, deadline_monotonic=None):
            assert identity is self.identity
            path.write_bytes(source_paths[self.source_index].read_bytes())
            os.chmod(path, 0o600)
            self.source_index += 1
            return self.identity

    screen = RepeatedLabelScreen()
    op = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=FakeClient(7, ["construction_prepared"] * 12),
        screen=screen,
        input_device=FakeInput(screen),
        receipt_root=tmp_path / "receipts",
    )
    op.receipt.begin()
    op._reservation_deadline = time.monotonic() + 30.0
    for _index in range(5):
        op._capture(
            "chat-repeated",
            crop=True,
            retain_chat_frame="typed_chat_readback",
            deadline_monotonic=time.monotonic() + 30.0,
        )
    assert op.receipt.attempt is not None
    retention_root = op.receipt.attempt / "chat-fullframes"
    assert retention_root.stat().st_mode & 0o777 == 0o700
    retained = sorted(retention_root.glob("*.png"))
    assert [path.name.split("-", 1)[0] for path in retained] == [
        "00000002",
        "00000003",
        "00000004",
        "00000005",
    ]
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in retained)
    assert not (retention_root / "00000001-chat-repeated.png").exists()
    assert len({path.name for path in retained}) == 4
    records = sorted(op._chat_retained_frames, key=lambda item: item["sequence"])
    assert [record["sequence"] for record in records] == [2, 3, 4, 5]
    assert all((record["width"], record["height"]) == (1280, 720) for record in records)
    assert all(
        (record["derived_crop_width"], record["derived_crop_height"]) == (64, 64) for record in records
    )
    assert len({record["derived_crop_filename"] for record in records}) == 4
    for record, source_path in zip(records, source_paths[1:], strict=True):
        retained_path = retention_root / record["filename"]
        assert retained_path.read_bytes() == source_path.read_bytes()
        assert record["original_sha256"] == hashlib.sha256(source_path.read_bytes()).hexdigest()
        crop_path = op.receipt.attempt / record["derived_crop_filename"]
        assert record["derived_crop_sha256"] == hashlib.sha256(crop_path.read_bytes()).hexdigest()


def test_chat_fullframe_retention_skips_new_frame_when_eviction_fails(tmp_path, monkeypatch):
    manifest, _construct_scene, restore_scene = make_scenes(tmp_path)
    source_paths = []
    with Image.open(restore_scene) as source_image:
        for index in range(5):
            source = source_image.copy()
            source.putpixel((index, 0), (40 + index, 50 + index, 60 + index))
            source_path = tmp_path / f"eviction-source-{index}.png"
            source.save(source_path, format="PNG")
            source_paths.append(source_path)

    class EvictionScreen(FakeScreen):
        def __init__(self):
            super().__init__(tmp_path, restore_scene, restore_scene)
            self.source_index = 0

        def capture(self, path, *, identity, deadline_monotonic=None):
            assert identity is self.identity
            path.write_bytes(source_paths[self.source_index].read_bytes())
            os.chmod(path, 0o600)
            self.source_index += 1
            return self.identity

    screen = EvictionScreen()
    op = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=FakeClient(7, ["construction_prepared"] * 12),
        screen=screen,
        input_device=FakeInput(screen),
        receipt_root=tmp_path / "receipts",
    )
    op.receipt.begin()
    op._reservation_deadline = time.monotonic() + 30.0
    for _index in range(4):
        op._capture(
            "chat-eviction",
            crop=True,
            retain_chat_frame="typed_chat_readback",
            deadline_monotonic=time.monotonic() + 30.0,
        )

    original_unlink = Path.unlink

    def fail_old_frame_unlink(self, *args, **kwargs):
        if self.name == "00000001-chat-eviction.png":
            raise OSError("retention eviction denied")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_old_frame_unlink)
    op._capture(
        "chat-eviction",
        crop=True,
        retain_chat_frame="typed_chat_readback",
        deadline_monotonic=time.monotonic() + 30.0,
    )

    assert op.receipt.attempt is not None
    retention_root = op.receipt.attempt / "chat-fullframes"
    retained = sorted(retention_root.glob("*.png"))
    assert [path.name.split("-", 1)[0] for path in retained] == [
        "00000001",
        "00000002",
        "00000003",
        "00000004",
    ]
    assert len(op._chat_retained_frames) == 4
    assert [record["sequence"] for record in op._chat_retained_frames] == [1, 2, 3, 4]
    assert op._last_chat_capture_record["retention_skipped"] is True
    assert op._last_chat_capture_record["retention_error"] == "OSError"
    assert op._last_chat_capture_record["retained"] is False
    assert (
        op._last_chat_capture_record["original_sha256"]
        == hashlib.sha256(source_paths[4].read_bytes()).hexdigest()
    )
    assert (op._last_chat_capture_record["width"], op._last_chat_capture_record["height"]) == (
        1280,
        720,
    )
    assert op._last_chat_capture_record["derived_crop_width"] == screen.chat_crop[2]
    assert op._last_chat_capture_record["derived_crop_height"] == screen.chat_crop[3]
    assert not (retention_root / "00000005-chat-eviction.png").exists()


def test_chat_retention_write_failure_preserves_capture_ocr_and_readback_failure(tmp_path, monkeypatch):
    manifest, _construct_scene, restore_scene = make_scenes(tmp_path)
    capture_calls = []
    ocr_calls = []
    state_calls = []

    class CountingScreen(FakeScreen):
        def capture(self, path, *, identity, deadline_monotonic=None):
            capture_calls.append(path.name)
            return super().capture(path, identity=identity, deadline_monotonic=deadline_monotonic)

    screen = CountingScreen(tmp_path, restore_scene, restore_scene)
    input_device = FakeInput(screen)

    def state(path):
        state_calls.append(path.name)
        return operator.CHAT_EDITOR_INACTIVE

    op = operator.NativeCalibrationOperator(
        chat_state=state,
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=FakeClient(7, ["construction_prepared", "construction_prepared"]),
        screen=screen,
        input_device=input_device,
        ocr=lambda path: ocr_calls.append(path.name) or ("" if "chat-closed" in path.name else "/wrong"),
        receipt_root=tmp_path / "receipts",
    )
    op.receipt.begin()
    op._binding = operator.ReservationBinding.from_record(
        FakeBinding().record(7), expected_generation=7, now=1.0
    )
    op._reservation_deadline = 1000000000000.0
    op._handshake = "construction_prepared"

    original_write_bytes = Path.write_bytes
    retention_writes = 0

    def fail_typed_retention_write(self, data):
        nonlocal retention_writes
        if self.parent.name == "chat-fullframes" and self.name.startswith("."):
            retention_writes += 1
            if retention_writes == 2:
                raise OSError("retention copy denied")
        return original_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", fail_typed_retention_write)
    with pytest.raises(operator.OperatorError, match="readback"):
        op._chat("/crowley agent defer", expected_state="construction_prepared")

    assert len(capture_calls) == 2
    assert len(state_calls) == 2
    assert len(ocr_calls) == 1
    assert ocr_calls[0].startswith("chat-") and "chat-closed" not in ocr_calls[0]
    assert input_device.calls == [
        ("key", "Return"),
        ("type", "/crowley agent defer"),
        ("key", "Escape"),
    ]
    assert op._last_chat_capture_record["retention_error"] == "OSError"
    assert op._last_chat_capture_record["retained"] is False
    assert len(op._chat_retained_frames) == 1
    assert op.receipt.attempt is not None
    diagnostic = op.receipt.attempt / "chat-readback-00000002.json"
    assert diagnostic.is_file()
    report = json.loads(diagnostic.read_text(encoding="utf-8"))
    assert report["observed_normalized"] == "/wrong"
    assert report["failed_typed_capture"]["retained"] is False
    assert report["failed_typed_capture"]["original_sha256"]
    op.input.close()


def test_chat_readback_diagnostic_errors_do_not_mask_actual_mismatch_cleanup(tmp_path, monkeypatch):
    manifest, _construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, restore_scene, restore_scene)
    input_device = FakeInput(screen)
    state_calls = 0

    def state(path):
        nonlocal state_calls
        state_calls += 1
        if state_calls == 1:
            return operator.CHAT_EDITOR_INACTIVE
        raise operator.OperatorError("typed classification failed")

    op = operator.NativeCalibrationOperator(
        chat_state=state,
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=FakeClient(7, ["construction_prepared", "construction_prepared"]),
        screen=screen,
        input_device=input_device,
        ocr=lambda path: "" if "chat-closed" in path.name else "/wrong",
        receipt_root=tmp_path / "receipts",
    )
    op.receipt.begin()
    op._binding = operator.ReservationBinding.from_record(
        FakeBinding().record(7), expected_generation=7, now=1.0
    )
    op._reservation_deadline = time.monotonic() + 30.0
    op._handshake = "construction_prepared"

    original_write_text = Path.write_text

    def fail_diagnostic_write(self, *args, **kwargs):
        if self.name.startswith(".") and self.name.endswith(".tmp"):
            raise OSError("diagnostic storage failed")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_diagnostic_write)
    with pytest.raises(operator.OperatorError, match="readback"):
        op._chat("/crowley agent defer", expected_state="construction_prepared")
    assert state_calls == 2
    assert ("key", "Escape") in input_device.calls
    assert ("key", "Return") not in input_device.calls[2:]
    assert op.receipt.attempt is not None
    assert not list(op.receipt.attempt.glob("chat-readback-*.json"))
    input_device.close()
    assert input_device.closed


def test_run_preserves_chat_mismatch_through_diagnostic_errors_and_auto_cleanup(tmp_path, monkeypatch):
    manifest, _construct_scene, restore_scene = make_scenes(tmp_path)
    closed_crop = _write_chat_ocr_fixture(tmp_path, "run-closed.png", _OCR_RELOAD_CLOSED_CROP_B64)
    typed_crop = _write_chat_ocr_fixture(tmp_path, "run-typed.png", _OCR_RELOAD_TYPED_CROP_B64)

    class ActualChatScreen(FakeScreen):
        def __init__(self):
            super().__init__(tmp_path, restore_scene, restore_scene)
            self.chat_crop = (0, 0, *operator.CHAT_CROP_SIZE)

        def capture(self, path, *, identity, deadline_monotonic=None):
            assert identity is self.identity
            source = closed_crop if "chat-closed" in path.name else typed_crop
            path.write_bytes(source.read_bytes())
            os.chmod(path, 0o600)
            return self.identity

    screen = ActualChatScreen()
    input_device = FakeInput(screen)
    state_calls = 0
    cancel_calls = []

    def state(path):
        nonlocal state_calls
        state_calls += 1
        if state_calls == 1:
            assert (
                operator.NativeCalibrationOperator._chat_editor_state(path) == operator.CHAT_EDITOR_INACTIVE
            )
            return operator.CHAT_EDITOR_INACTIVE
        assert operator.NativeCalibrationOperator._chat_editor_state(path) == operator.CHAT_EDITOR_ACTIVE
        raise operator.OperatorError("typed classification failed")

    op = operator.NativeCalibrationOperator(
        chat_state=state,
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=FakeClient(7, []),
        screen=screen,
        input_device=input_device,
        ocr=lambda path: "" if "chat-closed" in path.name else "/wrong",
        receipt_root=tmp_path / "receipts",
    )
    op._prepare = lambda *, preflight: None
    op._recheck = lambda expected_state, deadline_monotonic: None
    op._pause_after_terminal_error = lambda: False
    op._cancel_active_reservation = lambda: cancel_calls.append(True)
    op._reservation_deadline = time.monotonic() + 30.0

    original_write_text = Path.write_text

    def fail_diagnostic_write(self, *args, **kwargs):
        if self.name.startswith(".") and self.name.endswith(".tmp"):
            raise OSError("diagnostic storage failed")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_diagnostic_write)
    with pytest.raises(operator.OperatorError, match="readback"):
        op.run()
    assert state_calls == 2
    assert cancel_calls == [True]
    assert input_device.closed
    assert ("key", "Escape") in input_device.calls
    assert ("key", "Return") not in input_device.calls[2:]
    assert op.receipt.attempt is not None
    assert json.loads((op.receipt.attempt / "receipt.json").read_text())["outcome"] == "failure"
    assert not list(op.receipt.attempt.glob("chat-readback-*.json"))
    typed_records = [record for record in op._chat_retained_frames if record["use"] == "typed_chat_readback"]
    assert len(typed_records) == 1
    typed_record = typed_records[0]
    typed_capture = op.receipt.attempt / typed_record["derived_crop_filename"]
    assert typed_record["derived_crop_sha256"] == hashlib.sha256(typed_capture.read_bytes()).hexdigest()


def test_chat_readback_diagnostic_records_real_typed_editor_state_and_crop(tmp_path):
    manifest, _construct_scene, restore_scene = make_scenes(tmp_path)
    closed_crop = _write_chat_ocr_fixture(tmp_path, "real-closed.png", _OCR_RELOAD_CLOSED_CROP_B64)
    typed_crop = _write_chat_ocr_fixture(tmp_path, "real-typed.png", _OCR_RELOAD_TYPED_CROP_B64)

    class ActualChatScreen(FakeScreen):
        def __init__(self):
            super().__init__(tmp_path, restore_scene, restore_scene)
            self.chat_crop = (0, 0, *operator.CHAT_CROP_SIZE)

        def capture(self, path, *, identity, deadline_monotonic=None):
            assert identity is self.identity
            source = closed_crop if "chat-closed" in path.name else typed_crop
            path.write_bytes(source.read_bytes())
            os.chmod(path, 0o600)
            return self.identity

    screen = ActualChatScreen()
    input_device = FakeInput(screen)
    op = operator.NativeCalibrationOperator(
        chat_state=operator.NativeCalibrationOperator._chat_editor_state,
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=FakeClient(7, ["construction_prepared", "construction_prepared"]),
        screen=screen,
        input_device=input_device,
        ocr=lambda path: "" if "chat-closed" in path.name else "/wrong",
        receipt_root=tmp_path / "receipts",
    )
    op.receipt.begin()
    op._binding = operator.ReservationBinding.from_record(
        FakeBinding().record(7), expected_generation=7, now=1.0
    )
    op._reservation_deadline = 1000000000000.0
    op._handshake = "construction_prepared"

    with pytest.raises(operator.OperatorError, match="readback"):
        op._chat("/reload", expected_state="construction_prepared")

    assert op.receipt.attempt is not None
    diagnostic = op.receipt.attempt / "chat-readback-00000002.json"
    report = json.loads(diagnostic.read_text(encoding="utf-8"))
    failed = report["failed_typed_capture"]
    assert failed["sequence"] == 2
    assert failed["use"] == "typed_chat_readback"
    assert failed["typed_editor_state"] == operator.CHAT_EDITOR_ACTIVE
    typed_path = op.receipt.attempt / failed["derived_crop_filename"]
    assert failed["derived_crop_sha256"] == hashlib.sha256(typed_path.read_bytes()).hexdigest()
    assert input_device.calls == [
        ("key", "Return"),
        ("type", "/reload"),
        ("key", "Escape"),
    ]


def test_chat_readback_diagnostic_caps_text_and_survives_classification_or_write_errors(
    tmp_path, monkeypatch
):
    manifest, _construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, restore_scene, restore_scene)
    op = operator.NativeCalibrationOperator(
        chat_state=lambda _path: (_ for _ in ()).throw(operator.OperatorError("classification failed")),
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=FakeClient(7, []),
        screen=screen,
        input_device=FakeInput(screen),
        receipt_root=tmp_path / "receipts",
    )
    op.receipt.begin()
    op._reservation_deadline = time.monotonic() + 30.0
    typed = tmp_path / "typed.png"
    typed.write_bytes(restore_scene.read_bytes())
    op._last_chat_capture_record = {
        "sequence": 9,
        "label": "chat-repeat",
        "use": "typed_chat_readback",
        "original_sha256": "a" * 64,
        "derived_crop_sha256": "b" * 64,
    }
    long = "x" * 300
    op._record_chat_readback_diagnostic(
        command="/reload",
        typed_path=typed,
        observed_raw=long,
        observed=long,
        expected="/reload",
    )
    assert op.receipt.attempt is not None
    diagnostic = op.receipt.attempt / "chat-readback-00000009.json"
    report = json.loads(diagnostic.read_text(encoding="utf-8"))
    assert len(report["observed_raw"]) == 256
    assert report["observed_raw_truncated"] is True
    assert len(report["observed_normalized"]) == 256
    assert report["observed_normalized_truncated"] is True
    assert report["expected_truncated"] is False
    assert report["failed_typed_capture"]["typed_editor_state"] is None
    assert report["failed_typed_capture"]["typed_editor_state_error"] == "OperatorError"
    bounded, truncated = operator.NativeCalibrationOperator._bounded_chat_text("é" * 257)
    assert len(bounded) == 256 and bounded == "é" * 256 and truncated is True
    bounded, truncated = operator.NativeCalibrationOperator._bounded_chat_text("λ" * 256)
    assert len(bounded) == 256 and truncated is False

    original_write_text = Path.write_text

    def fail_diagnostic_write(self, *args, **kwargs):
        if self.name.startswith(".") and self.name.endswith(".tmp"):
            raise OSError("diagnostic storage failed")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_diagnostic_write)
    # Persistence failure remains best-effort and cannot replace the causal
    # readback refusal path (the Escape and cleanup assertions live above).
    op._record_chat_readback_diagnostic(
        command="/reload",
        typed_path=typed,
        observed_raw="wrong",
        observed="wrong",
        expected="/reload",
    )
    op.input.close()


def test_control_preflight_ordinary_reload_branch_is_fixed_and_no_resume(tmp_path):
    manifest, _construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, restore_scene, restore_scene)
    input_device = FakeInput(screen)
    client = FakeClient(7, ["recovery_required"] * 8)
    op = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_RECOVER,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=client,
        screen=screen,
        input_device=input_device,
        ocr=lambda path: "" if "chat-closed" in path.name else "/reload",
        receipt_root=tmp_path / "receipts",
    )
    result = op.run()
    assert result["ok"] is True
    assert ("type", "/reload") in input_device.calls
    assert not any(call == ("type", "/crowley agent recover") for call in input_device.calls)
    assert not any(call == ("type", "/crowley agent resume") for call in input_device.calls)


def test_terminal_pause_requires_strictly_newer_generation(tmp_path):
    manifest, _construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, restore_scene, restore_scene)

    class ErrorClient(FakeClient):
        def status(self, *, deadline=None):
            return {
                "ok": True,
                "status": {
                    "state": "NATIVE_CALIBRATION",
                    "generation": self.generation,
                    "calibrationOperation": {
                        "state": "error",
                        "kind": "native",
                        "generation": self.generation,
                    },
                },
            }

        def pause(self, generation: int, *, deadline=None):
            self.calls.append({"command": "pause", "generation": generation})
            return {
                "ok": True,
                "status": {
                    "state": "PAUSED",
                    "generation": self.generation,
                    "calibrationOperation": {"state": "idle", "kind": "native"},
                    "guardian": {"armed": False},
                },
            }

    client = ErrorClient(7, [])
    candidate = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_MEASURE_DEFERRED,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=client,
        screen=screen,
        input_device=FakeInput(screen),
    )
    candidate._calibration_started = True
    candidate._calibration_terminal_error = True
    assert candidate._pause_after_terminal_error() is False
    assert any(call["command"] == "pause" for call in client.calls)


def test_calibration_error_is_observed_before_construction_recheck(tmp_path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, construct_scene, restore_scene)

    class ErrorStatusClient(FakeClient):
        def calibration_status(self, *, deadline=None):
            return {
                "ok": True,
                "status": {
                    "state": "NATIVE_CALIBRATION",
                    "generation": self.generation,
                    "calibrationOperation": {
                        "kind": "native",
                        "generation": self.generation,
                        "state": "error",
                        "error": "original daemon failure",
                    },
                },
            }

        def recheck(self, *args, **kwargs):
            raise AssertionError("terminal error must precede construction recheck")

    candidate = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_MEASURE_DEFERRED,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=ErrorStatusClient(7, []),
        screen=screen,
        input_device=FakeInput(screen),
    )
    candidate._calibration_started = True
    candidate._reservation_deadline = time.monotonic() + 5.0
    with pytest.raises(operator.OperatorError, match="original daemon failure"):
        candidate._await_calibration_terminal()
    assert candidate._calibration_terminal_error is True


def test_terminal_failure_recheck_preserves_exact_binding_before_pause(tmp_path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    candidate = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_MEASURE_DEFERRED,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=FakeClient(7, []),
        screen=FakeScreen(tmp_path, construct_scene, restore_scene),
        input_device=FakeInput(FakeScreen(tmp_path, construct_scene, restore_scene)),
    )
    original = operator.ReservationBinding.from_record(FakeBinding(state="consumed").record(7), now=1.0)
    candidate._binding = original
    candidate._handshake = "running"
    candidate._state_history = [("construction_prepared", 1.0), ("running", 2.0)]
    failed_record = FakeBinding(state="restoration_required").record(7)
    reply = {
        "ok": True,
        "reservation": failed_record,
        "handshake": {
            "state": "restoration_required",
            "failure": "worker failed",
            "stateHistory": [
                {"state": "construction_prepared", "nativeClock": 1.0},
                {"state": "running", "nativeClock": 2.0},
                {"state": "restoration_required", "nativeClock": 3.0},
            ],
        },
    }
    with pytest.raises(operator.OperatorError, match="worker failed"):
        candidate._adopt_reply(reply)
    assert candidate._calibration_terminal_error is True
    assert candidate._binding == original


def test_control_preflight_runs_actual_construct_then_ordinary_recovery(tmp_path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, construct_scene, restore_scene)
    input_device = FakeInput(screen)
    client = FakeClient(7, [], kind="preflight")
    input_device.dispatch = client.dispatch
    commands = iter(["/crowley agent defer", "/reload"])
    op = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=client,
        screen=screen,
        input_device=input_device,
        ocr=lambda path: "" if "chat-closed" in path.name else next(commands),
        receipt_root=tmp_path / "receipts",
        sleep=lambda _delay: None,
    )
    result = op.run()
    assert result["ok"] is True
    assert [value for kind, value in input_device.calls if kind == "type"] == [
        "/crowley agent defer",
        "/reload",
    ]
    assert any(kind == "click" and value == "construct" for kind, value in input_device.calls)
    assert any(call.get("command") == "recovery_begin" for call in client.calls)
    assert any(call.get("command") == "recovery_complete" for call in client.calls)


def test_recover_restore_branch_uses_actual_restore_click(tmp_path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, construct_scene, restore_scene)
    input_device = FakeInput(screen)
    client = FakeClient(7, [], recovery_kind="restore")
    input_device.dispatch = client.dispatch
    op = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_RECOVER,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=client,
        screen=screen,
        input_device=input_device,
        ocr=lambda path: "" if "chat-closed" in path.name else "/crowley agent recover",
        receipt_root=tmp_path / "receipts",
        sleep=lambda _delay: None,
    )
    result = op.run()
    assert result["ok"] is True
    assert ("type", "/crowley agent recover") in input_device.calls
    assert any(kind == "click" and value == "restore" for kind, value in input_device.calls)


def test_handshake_skip_is_refused(tmp_path):
    manifest, _construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, restore_scene, restore_scene)
    input_device = FakeInput(screen)
    client = FakeClient(7, [])
    op = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_RECOVER,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=client,
        screen=screen,
        input_device=input_device,
        receipt_root=tmp_path / "receipts",
    )
    op.receipt.begin()
    op._binding = operator.ReservationBinding.from_record(
        FakeBinding().record(7), expected_generation=7, now=1.0
    )
    op._handshake = "construction_prepared"
    client.current_state = "baseline_bound"
    client.states = ["baseline_bound"]
    with pytest.raises(operator.OperatorError, match="skipped"):
        op._recheck()


def test_state_history_skip_is_refused_before_input(tmp_path):
    manifest, _construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, restore_scene, restore_scene)
    op = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_MEASURE_DEFERRED,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=FakeClient(7, []),
        screen=screen,
        input_device=FakeInput(screen),
        receipt_root=tmp_path / "receipts",
    )
    op.receipt.begin()
    reply = FakeClient(7, []).prepare(7)
    reply["handshake"] = {
        "state": "baseline_bound",
        "stateHistory": [
            {"state": "construction_prepared", "nativeClock": 1.0},
            {"state": "baseline_bound", "nativeClock": 2.0},
        ],
    }
    with pytest.raises(operator.OperatorError, match="skipped"):
        op._adopt_reply(reply)
    op.input.close()


def test_owned_emitter_gate_and_release_subprocess(tmp_path):
    log = tmp_path / "events.log"
    script = tmp_path / "pinned-emitter"
    script.write_text(f"#!/bin/sh\nprintf '%s\n' \"$*\" >> {log}\n")
    script.chmod(0o700)
    fd = os.open(script, os.O_RDONLY)

    class Tool:
        proc_path = f"/proc/self/fd/{fd}"

        class Image:
            pass

        image = Image()
        image.fd = fd

    emitter = operator.OwnedEmitter(Tool(), context=multiprocessing.get_context("fork"))
    try:
        emitter.start()
        assert emitter.pgid and emitter.pgid > 0
        emitter.press()
        emitter.release()
    finally:
        emitter.abort()
        os.close(fd)
    assert log.read_text().splitlines() == ["mousedown 1", "mouseup 1"]


def test_owned_emitter_gate_eof_is_bounded(tmp_path):
    script = tmp_path / "pinned-emitter"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o700)
    fd = os.open(script, os.O_RDONLY)

    class Tool:
        proc_path = f"/proc/self/fd/{fd}"

        class Image:
            pass

        image = Image()
        image.fd = fd

    emitter = operator.OwnedEmitter(Tool(), context=multiprocessing.get_context("fork"))
    try:
        emitter.start()
        os.close(emitter._gate_write)
        emitter._gate_write = -1
        with pytest.raises((EOFError, TimeoutError, operator.OperatorError)):
            emitter.press()
    finally:
        emitter.abort()
        os.close(fd)


def test_spawned_sealed_emitter_uses_real_pinned_image_and_acknowledges_release():
    tool = operator.PinnedInputTool.load()
    emitter = operator.OwnedEmitter(
        tool,
        action=("--help",),
        release_action=(),
        environment={},
    )
    try:
        emitter.start()
        assert emitter.pgid and emitter.pgid > 0
        emitter.press()
        emitter.release()
        assert emitter._process is not None
        assert not emitter._process.is_alive()
    finally:
        emitter.abort()
        tool.close()


def test_spawned_watchdog_entry_acknowledges_owned_state_and_empty_clear():
    tool = operator.PinnedInputTool.load()
    watchdog = operator.Watchdog(tool, interval=0.02, environment={})
    try:
        watchdog.publish(groups=set(), buttons=set(), keys={"Return"})
        watchdog.clear()
        assert watchdog._process.is_alive()
    finally:
        watchdog.close()
        tool.close()
    assert not watchdog._process.is_alive()


def test_watchdog_and_cancellation_actor_have_independent_sessions():
    tool = operator.PinnedInputTool.load()
    watchdog = operator.Watchdog(tool, interval=0.02, environment={})
    actor = operator.CancellationActor(
        Path("/tmp/native-calibration-unused.sock"),
        generation=7,
        reservation_id="session-check",
        deadline=time.monotonic() + 8.0,
        owner_pid=os.getpid(),
        owner_start=operator._proc_start(os.getpid()),
    )
    actor.start()
    try:
        assert watchdog._process is not None and watchdog._process.pid is not None
        assert actor._process is not None and actor._process.pid is not None
        assert os.getsid(watchdog._process.pid) == watchdog._process.pid
        assert os.getsid(actor._process.pid) == actor._process.pid
    finally:
        actor.close()
        watchdog.close()
        tool.close()


def test_spawned_watchdog_ack_loss_fails_closed_and_retains_actor():
    tool = operator.PinnedInputTool.load()
    watchdog = operator.Watchdog(tool, interval=0.02, environment={})
    try:
        os.close(watchdog._ack_read)
        watchdog._ack_read = -1
        with pytest.raises(operator.OperatorError, match="acknowledgement"):
            watchdog.publish(groups=set(), buttons=set(), keys=set())
        assert watchdog._process.is_alive()
    finally:
        watchdog.close()
        tool.close()


def test_unconfirmed_release_retains_actor_and_owner_close_is_bounded(tmp_path):
    artifact = make_recording_artifact(tmp_path)
    from crowley.session.input_tool import PRIVATE_TOOL_BASENAME

    executable = artifact / PRIVATE_TOOL_BASENAME
    delayed_source = tmp_path / "delayed.c"
    delayed_binary = tmp_path / "delayed"
    release_marker = tmp_path / "release-first-attempt"
    release_log = tmp_path / "release-log"
    delayed_source.write_text(
        """
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
    const char *marker = getenv("CROWLEY_RELEASE_MARKER");
    const char *log = getenv("CROWLEY_RELEASE_LOG");
    if (argc > 1 && (argv[1][0] == 'k' || argv[1][0] == 'm') && marker && access(marker, F_OK) != 0) {
        int fd = open(marker, O_WRONLY | O_CREAT | O_EXCL, 0600);
        if (fd >= 0) close(fd);
        usleep(4000000);
        return 1;
    }
    if (argc > 1 && (argv[1][0] == 'k' || argv[1][0] == 'm') && marker && access(marker, F_OK) == 0)
        usleep(2000000);
    if (argc > 1 && log) {
        int fd = open(log, O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (fd >= 0) {
            write(fd, argv[1], strlen(argv[1]));
            write(fd, "\\n", 1);
            close(fd);
        }
    }
    return 0;
}
""",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(delayed_source), "-o", str(delayed_binary)], check=True)
    executable.unlink()
    shutil.copyfile(delayed_binary, executable)
    executable.chmod(0o700)
    manifest = artifact / "manifest.json"
    record = json.loads(manifest.read_text(encoding="utf-8"))
    record["executable_sha256"] = hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    tool = operator.PinnedInputTool.from_root(artifact)
    watchdog = operator.Watchdog(
        tool,
        interval=0.02,
        environment={
            "CROWLEY_RELEASE_MARKER": str(release_marker),
            "CROWLEY_RELEASE_LOG": str(release_log),
        },
    )
    try:
        watchdog.publish(groups=set(), buttons=set(), keys={"Return"})
        started = time.monotonic()
        with pytest.raises(operator.OperatorError, match="confirm release"):
            watchdog.close()
        assert time.monotonic() - started < 4.5
        assert watchdog._process.is_alive()
        deadline = time.monotonic() + 8.0
        while watchdog._process.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not watchdog._process.is_alive()
        assert release_marker.is_file()
        assert release_log.read_text(encoding="utf-8") == "keyup\n"
    finally:
        try:
            watchdog.close()
        except Exception:
            pass
        tool.close()


def test_real_owner_exit_is_bounded_while_failed_release_actor_survives(tmp_path):
    artifact = make_recording_artifact(tmp_path)
    from crowley.session.input_tool import PRIVATE_TOOL_BASENAME

    executable = artifact / PRIVATE_TOOL_BASENAME
    source = tmp_path / "persistent-release.c"
    binary = tmp_path / "persistent-release"
    source.write_text(
        """
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
    const char *enable = getenv("CROWLEY_RELEASE_ENABLE");
    const char *log = getenv("CROWLEY_RECORDER");
    if (argc > 1 && strcmp(argv[1], "keyup") == 0 && (!enable || access(enable, F_OK) != 0)) {
        usleep(5000000);
        return 1;
    }
    if (argc > 1 && log) {
        int fd = open(log, O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (fd >= 0) {
            write(fd, argv[1], strlen(argv[1]));
            write(fd, "\\n", 1);
            close(fd);
        }
    }
    return 0;
}
""",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(source), "-o", str(binary)], check=True)
    executable.unlink()
    shutil.copyfile(binary, executable)
    executable.chmod(0o700)
    manifest = artifact / "manifest.json"
    record = json.loads(manifest.read_text(encoding="utf-8"))
    record["executable_sha256"] = hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")

    release_marker = tmp_path / "persistent-release-actions"
    enable_marker = tmp_path / "enable-release"
    status_read, status_write = multiprocessing.get_context("spawn").Pipe(duplex=False)
    owner = multiprocessing.get_context("spawn").Process(
        target=_persistent_release_owner_fixture,
        args=(str(artifact), str(release_marker), str(enable_marker), status_write),
        name="native-calibration-persistent-release-owner",
        daemon=False,
    )
    owner.start()
    status_write.close()
    watchdog_pid = watchdog_start = None
    try:
        assert status_read.poll(5.0), "persistent-release owner did not publish ownership"
        watchdog_pid, watchdog_start = status_read.recv()
        kill_started = time.monotonic()
        owner.kill()
        owner.join(timeout=5.0)
        assert not owner.is_alive()
        assert owner.exitcode == -operator.signal.SIGKILL
        assert time.monotonic() - kill_started < 5.0
        assert watchdog_pid and watchdog_start
        time.sleep(0.5)
        assert not _wait_pid_identity_exit(watchdog_pid, watchdog_start, 0.0)
        enable_marker.touch(mode=0o600)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if release_marker.exists() and release_marker.read_text(encoding="utf-8").splitlines() == [
                "keyup"
            ]:
                break
            time.sleep(0.05)
        assert release_marker.read_text(encoding="utf-8").splitlines() == ["keyup"]
        assert _wait_pid_identity_exit(watchdog_pid, watchdog_start, 5.0)
    finally:
        if owner.is_alive():
            owner.kill()
        owner.join(timeout=2.0)
        status_read.close()


def test_normal_systemexit_keeps_failed_release_actor_alive_until_enabled(tmp_path):
    artifact = make_recording_artifact(tmp_path)
    from crowley.session.input_tool import PRIVATE_TOOL_BASENAME

    executable = artifact / PRIVATE_TOOL_BASENAME
    source = tmp_path / "normal-exit-release.c"
    binary = tmp_path / "normal-exit-release"
    source.write_text(
        """
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
    const char *enable = getenv("CROWLEY_RELEASE_ENABLE");
    const char *marker = getenv("CROWLEY_RELEASE_MARKER");
    if (argc > 1 && strcmp(argv[1], "keyup") == 0) {
        for (int i = 0; i < 120 && (!enable || access(enable, F_OK) != 0); ++i)
            usleep(50000);
        if (!enable || access(enable, F_OK) != 0) return 1;
    }
    if (argc > 1 && marker) {
        int fd = open(marker, O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (fd >= 0) { write(fd, argv[1], strlen(argv[1])); write(fd, "\\n", 1); close(fd); }
    }
    return 0;
}
""",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(source), "-o", str(binary)], check=True)
    executable.unlink()
    shutil.copyfile(binary, executable)
    executable.chmod(0o700)
    manifest = artifact / "manifest.json"
    record = json.loads(manifest.read_text(encoding="utf-8"))
    record["executable_sha256"] = hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")

    release_marker = tmp_path / "normal-exit-release-actions"
    enable_marker = tmp_path / "normal-exit-enable"
    context = multiprocessing.get_context("spawn")
    status_read, status_write = context.Pipe(duplex=False)
    owner = context.Process(
        target=_normal_exit_failed_release_fixture,
        args=(str(artifact), str(release_marker), str(enable_marker), status_write),
        name="native-calibration-normal-systemexit-owner",
        daemon=False,
    )
    owner.start()
    status_write.close()
    try:
        assert status_read.poll(5.0), "normal SystemExit owner did not publish actor"
        watchdog_pid, watchdog_start = status_read.recv()
        owner.join(timeout=5.0)
        assert not owner.is_alive()
        assert owner.exitcode == 2
        assert watchdog_pid and watchdog_start
        assert not _wait_pid_identity_exit(watchdog_pid, watchdog_start, 0.0)
        enable_marker.touch(mode=0o600)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if release_marker.exists() and release_marker.read_text(encoding="utf-8").splitlines() == [
                "keyup"
            ]:
                break
            time.sleep(0.05)
        assert release_marker.read_text(encoding="utf-8").splitlines() == ["keyup"]
        assert _wait_pid_identity_exit(watchdog_pid, watchdog_start, 5.0)
    finally:
        if owner.is_alive():
            owner.kill()
        owner.join(timeout=2.0)
        status_read.close()


@pytest.mark.parametrize(
    "signum",
    [operator.signal.SIGTERM, operator.signal.SIGHUP, operator.signal.SIGINT, operator.signal.SIGKILL],
)
def test_spawned_emitter_group_death_is_observed_before_press(signum):
    tool = operator.PinnedInputTool.load()
    emitter = operator.OwnedEmitter(
        tool,
        action=("--help",),
        release_action=(),
        environment={},
    )
    try:
        emitter.start()
        assert emitter.pgid and emitter.pgid > 0
        os.killpg(emitter.pgid, signum)
        with pytest.raises(operator.OperatorError, match="press acknowledgement"):
            emitter.press()
    finally:
        emitter.abort()
        tool.close()


def test_spawned_emitter_gate_eof_with_real_sealed_image_is_bounded():
    tool = operator.PinnedInputTool.load()
    emitter = operator.OwnedEmitter(tool, action=("--help",), release_action=(), environment={})
    try:
        emitter.start()
        os.close(emitter._gate_write)
        with pytest.raises(operator.OperatorError, match="press acknowledgement"):
            emitter.press()
    finally:
        emitter.abort()
        tool.close()


def test_spawned_emitter_abort_reaps_late_child():
    tool = operator.PinnedInputTool.load()
    emitter = operator.OwnedEmitter(tool, action=("--help",), release_action=(), environment={})
    try:
        emitter.start()
        process = emitter._process
        assert process is not None and process.is_alive()
        emitter.abort()
        assert not process.is_alive()
    finally:
        emitter.abort()
        tool.close()


def test_spawned_owner_kill_during_confirmed_key_hold_releases_exact_action(tmp_path):
    source = tmp_path / "recorder.c"
    executable = tmp_path / "recorder"
    source.write_text(
        """
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
int main(int argc, char **argv) {
    const char *path = getenv("CROWLEY_RECORDER");
    if (path == NULL || argc < 3) return 2;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd < 0) return 3;
    dprintf(fd, "%s %s\\n", argv[1], argv[2]);
    close(fd);
    return 0;
}
""",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(source), "-o", str(executable)], check=True)
    from crowley.session.input_tool import (
        ARCHIVE_BASENAME,
        MANIFEST_BASENAME,
        PINNED_ARCHIVE_SHA256,
        PINNED_COMMIT,
        PRIVATE_TOOL_BASENAME,
    )

    artifact = tmp_path / "sealed-tool"
    artifact.mkdir(mode=0o700)
    shutil.copyfile(executable, artifact / PRIVATE_TOOL_BASENAME)
    shutil.copyfile(
        MODULE_PATH.parents[1] / "vendor" / "xdotool-source.tar.gz",
        artifact / ARCHIVE_BASENAME,
    )
    executable_sha256 = hashlib.sha256((artifact / PRIVATE_TOOL_BASENAME).read_bytes()).hexdigest()
    (artifact / MANIFEST_BASENAME).write_text(
        json.dumps(
            {
                "commit": PINNED_COMMIT,
                "archive_sha256": PINNED_ARCHIVE_SHA256,
                "executable": PRIVATE_TOOL_BASENAME,
                "executable_sha256": executable_sha256,
                pins.XDOTOOL_BUILD_INPUTS_KEY: _TEST_XDOTOOL_BUILD_INPUTS,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (artifact / PRIVATE_TOOL_BASENAME).chmod(0o700)
    (artifact / ARCHIVE_BASENAME).chmod(0o600)
    (artifact / MANIFEST_BASENAME).chmod(0o600)

    marker = tmp_path / "released-actions"
    tool = operator.PinnedInputTool.from_root(artifact)
    watchdog = operator.Watchdog(tool, interval=0.02, environment={"CROWLEY_RECORDER": str(marker)})
    emitter = operator.OwnedEmitter(
        tool,
        action=("keydown", "Return"),
        release_action=("keyup", "Return"),
        environment={"CROWLEY_RECORDER": str(marker)},
    )
    try:
        emitter.start()
        assert emitter.pgid and emitter.pgid > 0
        watchdog.publish(groups={emitter.pgid}, buttons=set(), keys={"Return"})
        emitter.press()
        process = emitter._process
        assert process is not None and process.is_alive()
        os.killpg(emitter.pgid, operator.signal.SIGKILL)
        emitter.abort()
        watchdog.close()
        assert not process.is_alive()
        assert marker.read_text(encoding="utf-8").splitlines() == [
            "keydown Return",
            "keyup Return",
        ]
    finally:
        emitter.abort()
        try:
            watchdog.close()
        except Exception:
            pass
        tool.close()


@pytest.mark.parametrize(
    ("action", "release", "expected"),
    [
        (("keydown", "Return"), ("keyup", "Return"), "keydown Return\nkeyup Return"),
        (("mousedown", "1"), ("mouseup", "1"), "mousedown 1\nmouseup 1"),
    ],
)
def test_actual_helper_owner_sigkill_releases_key_or_button_via_independent_watchdog(
    tmp_path, action, release, expected
):
    artifact = make_recording_artifact(tmp_path)
    marker = tmp_path / "owner-release-actions"
    context = multiprocessing.get_context("spawn")
    status_read, status_write = multiprocessing.Pipe(duplex=False)
    owner = context.Process(
        target=_owner_hold_fixture,
        args=(str(artifact), str(marker), action, release, status_write),
        name="native-calibration-owner-fixture",
        daemon=False,
    )
    owner.start()
    status_write.close()
    watchdog_pid = None
    watchdog_start = None
    emitter_pgid = None
    try:
        assert status_read.poll(5.0), "owner fixture did not publish confirmed hold"
        watchdog_pid, watchdog_start, emitter_pgid = status_read.recv()
        assert owner.is_alive()
        kill_started = time.monotonic()
        owner.kill()
        owner.join(timeout=5.0)
        assert not owner.is_alive()
        assert owner.exitcode == -operator.signal.SIGKILL
        assert time.monotonic() - kill_started < 5.0
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if marker.exists() and marker.read_text(encoding="utf-8").splitlines() == expected.splitlines():
                break
            time.sleep(0.05)
        assert marker.read_text(encoding="utf-8").splitlines() == expected.splitlines()
        assert watchdog_pid and watchdog_pid > 0
        assert watchdog_start
        assert _wait_pid_identity_exit(watchdog_pid, watchdog_start, 5.0)
        assert emitter_pgid and emitter_pgid > 0
        group_deadline = time.monotonic() + 5.0
        while time.monotonic() < group_deadline:
            try:
                os.killpg(emitter_pgid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail(f"owned emitter process group {emitter_pgid} was not reaped")
    finally:
        if owner.is_alive():
            owner.kill()
        owner.join(timeout=2.0)
        status_read.close()


def test_production_main_lifetime_arms_release_and_cancel_for_button_hold(tmp_path):
    manifest, construct_scene, _restore_scene = make_scenes(tmp_path)
    templates_path = tmp_path / "main-templates.json"
    templates_path.write_text(
        json.dumps(
            {
                "construct": {
                    "path": str(manifest.construct.path),
                    "sha256": manifest.construct.sha256,
                    "threshold": 5.0,
                },
                "restore": {
                    "path": str(manifest.restore.path),
                    "sha256": manifest.restore.sha256,
                    "threshold": 5.0,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    templates_path.chmod(0o600)
    config_path = tmp_path / "main-config.toml"
    config_path.write_text("test-only", encoding="utf-8")
    config_path.chmod(0o600)
    artifact = make_recording_artifact(tmp_path)
    release_marker = tmp_path / "main-release-actions"
    gate_marker = tmp_path / "main-button-gate"
    error_path = tmp_path / "main-error"
    watchdog_info = tmp_path / "main-watchdog-info"
    socket_path = tmp_path / "main-session.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    received: list[dict] = []

    def serve_once():
        connection, _address = listener.accept()
        with connection:
            chunks = []
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            received.append(json.loads(b"".join(chunks).decode("utf-8")))
            connection.sendall(b'{"ok":true}\n')

    import threading

    server_thread = threading.Thread(target=serve_once, daemon=True)
    server_thread.start()
    context = multiprocessing.get_context("spawn")
    owner = context.Process(
        target=_production_main_owner_fixture,
        args=(
            str(artifact),
            str(release_marker),
            str(gate_marker),
            str(socket_path),
            str(construct_scene),
            str(config_path),
            str(templates_path),
            str(error_path),
            "",
            str(watchdog_info),
        ),
        name="native-calibration-production-main-owner",
        daemon=False,
    )
    owner.start()
    try:
        deadline = time.monotonic() + 8.0
        while not gate_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert gate_marker.exists(), (
            error_path.read_text(encoding="utf-8")
            if error_path.exists()
            else "production main did not reach acknowledged button hold"
        )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if (
                release_marker.exists()
                and "mousedown 1" in release_marker.read_text(encoding="utf-8").splitlines()
            ):
                break
            time.sleep(0.05)
        assert "mousedown 1" in release_marker.read_text(encoding="utf-8").splitlines()
        owner.kill()
        server_thread.join(timeout=8.0)
        assert not server_thread.is_alive()
        _reap_process(owner, timeout=5.0)
        assert not owner.is_alive()
        assert owner.exitcode == -operator.signal.SIGKILL
        assert received == [
            {
                "command": "native_construction_recovery_cancel",
                "generation": 7,
                "reservationId": "reservation-1",
            }
        ]
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if release_marker.exists() and release_marker.read_text(encoding="utf-8").splitlines()[-1:] == [
                "mouseup 1"
            ]:
                break
            time.sleep(0.05)
        lines = release_marker.read_text(encoding="utf-8").splitlines()
        assert "mousedown 1" in lines
        assert lines[-1] == "mouseup 1"
    finally:
        _reap_process(owner)
        if watchdog_info.exists():
            pid_text, start = watchdog_info.read_text(encoding="utf-8").split(" ", 1)
            assert _wait_pid_identity_exit(int(pid_text), start, 8.0)
        server_thread.join(timeout=2.0)
        listener.close()


def test_production_main_handled_term_returns_nonzero_and_retains_release_actor(tmp_path):
    manifest, construct_scene, _restore_scene = make_scenes(tmp_path)
    templates_path = tmp_path / "main-term-templates.json"
    templates_path.write_text(
        json.dumps(
            {
                "construct": {
                    "path": str(manifest.construct.path),
                    "sha256": manifest.construct.sha256,
                    "threshold": 5.0,
                },
                "restore": {
                    "path": str(manifest.restore.path),
                    "sha256": manifest.restore.sha256,
                    "threshold": 5.0,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    templates_path.chmod(0o600)
    config_path = tmp_path / "main-term-config.toml"
    config_path.write_text("test-only", encoding="utf-8")
    config_path.chmod(0o600)
    artifact = make_recording_artifact(tmp_path)
    from crowley.session.input_tool import PRIVATE_TOOL_BASENAME

    executable = artifact / PRIVATE_TOOL_BASENAME
    source = tmp_path / "term-release.c"
    binary = tmp_path / "term-release"
    source.write_text(
        """
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int main(int argc, char **argv) {
    const char *enable = getenv("CROWLEY_RELEASE_ENABLE");
    const char *marker = getenv("CROWLEY_RECORDER");
    if (argc > 1 && strcmp(argv[1], "mouseup") == 0) {
        for (int i = 0; i < 120 && (!enable || access(enable, F_OK) != 0); ++i)
            usleep(50000);
        if (!enable || access(enable, F_OK) != 0) return 1;
    }
    if (argc > 1 && marker) {
        int fd = open(marker, O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (fd >= 0) {
            write(fd, argv[1], strlen(argv[1]));
            if (strcmp(argv[1], "sleep") == 0) {
                for (int i = 2; i < argc; i++) {
                    write(fd, " ", 1);
                    write(fd, argv[i], strlen(argv[i]));
                }
            }
            write(fd, "\\n", 1);
            close(fd);
        }
    }
    return 0;
}
""",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(source), "-o", str(binary)], check=True)
    executable.unlink()
    shutil.copyfile(binary, executable)
    executable.chmod(0o700)
    record = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    record["executable_sha256"] = hashlib.sha256(executable.read_bytes()).hexdigest()
    (artifact / "manifest.json").write_text(json.dumps(record, sort_keys=True), encoding="utf-8")

    release_marker = tmp_path / "main-term-release-actions"
    enable_marker = tmp_path / "main-term-enable-release"
    gate_marker = tmp_path / "main-term-button-gate"
    error_path = tmp_path / "main-term-error"
    watchdog_info = tmp_path / "main-term-watchdog-info"
    socket_path = tmp_path / "main-term-session.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    received: list[dict] = []

    def serve_once():
        connection, _address = listener.accept()
        with connection:
            chunks = []
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            received.append(json.loads(b"".join(chunks).decode("utf-8")))
            connection.sendall(b'{"ok":true}\n')

    import threading

    server_thread = threading.Thread(target=serve_once, daemon=True)
    server_thread.start()
    context = multiprocessing.get_context("spawn")
    owner = context.Process(
        target=_production_main_owner_fixture,
        args=(
            str(artifact),
            str(release_marker),
            str(gate_marker),
            str(socket_path),
            str(construct_scene),
            str(config_path),
            str(templates_path),
            str(error_path),
            str(enable_marker),
            str(watchdog_info),
        ),
        name="native-calibration-production-main-term-owner",
        daemon=False,
    )
    owner.start()
    try:
        deadline = time.monotonic() + 8.0
        while not gate_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert gate_marker.exists(), (
            error_path.read_text(encoding="utf-8") if error_path.exists() else "no button gate"
        )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if (
                release_marker.exists()
                and "mousedown" in release_marker.read_text(encoding="utf-8").splitlines()
            ):
                break
            time.sleep(0.05)
        assert "mousedown" in release_marker.read_text(encoding="utf-8").splitlines()
        started = time.monotonic()
        os.kill(owner.pid, operator.signal.SIGTERM)
        time.sleep(0.15)
        if owner.is_alive():
            os.kill(owner.pid, operator.signal.SIGINT)
            os.kill(owner.pid, operator.signal.SIGHUP)
        owner.join(timeout=8.0)
        assert not owner.is_alive()
        assert owner.exitcode == 2
        assert time.monotonic() - started < 8.0
        server_thread.join(timeout=8.0)
        assert not server_thread.is_alive()
        assert received == [
            {
                "command": "native_construction_recovery_cancel",
                "generation": 7,
                "reservationId": "reservation-1",
            }
        ]
        enable_marker.touch(mode=0o600)
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if release_marker.exists() and release_marker.read_text(encoding="utf-8").splitlines()[-1:] == [
                "mouseup"
            ]:
                break
            time.sleep(0.05)
        assert release_marker.read_text(encoding="utf-8").splitlines()[-1:] == ["mouseup"]
    finally:
        _reap_process(owner)
        if watchdog_info.exists():
            pid_text, start = watchdog_info.read_text(encoding="utf-8").split(" ", 1)
            assert _wait_pid_identity_exit(int(pid_text), start, 8.0)
        server_thread.join(timeout=2.0)
        listener.close()


def test_actual_cancellation_actor_sends_exact_admitted_ipc_after_owner_sigkill(tmp_path):
    socket_path = tmp_path / "session.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    received: list[dict] = []

    def serve_once():
        connection, _address = listener.accept()
        with connection:
            chunks = []
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            received.append(json.loads(b"".join(chunks).decode("utf-8")))
            connection.sendall(b'{"ok":true}\n')

    import threading

    server_thread = threading.Thread(target=serve_once, daemon=True)
    server_thread.start()
    context = multiprocessing.get_context("spawn")
    status_read, status_write = context.Pipe(duplex=False)
    owner = context.Process(
        target=_cancellation_owner_fixture,
        args=(str(socket_path), status_write),
        name="native-calibration-cancellation-owner-fixture",
    )
    owner.start()
    status_write.close()
    actor_pid = actor_start = None
    try:
        assert status_read.poll(5.0), "cancellation owner did not start its actor"
        actor_pid, actor_start = status_read.recv()
        assert actor_pid and actor_pid > 0
        kill_started = time.monotonic()
        owner.kill()
        server_thread.join(timeout=5.0)
        assert not server_thread.is_alive()
        assert time.monotonic() - kill_started < 5.0
        assert received == [
            {
                "command": "native_construction_recovery_cancel",
                "generation": 17,
                "reservationId": "reservation-live",
            }
        ]
        owner.join(timeout=5.0)
        assert not owner.is_alive()
    finally:
        _reap_process(owner)
        if actor_pid:
            assert _wait_pid_identity_exit(actor_pid, actor_start, 5.0)
        status_read.close()
        listener.close()


def test_actor_owns_prepare_bootstrap_finalize_recheck_cancel_via_peer_credentials(tmp_path):
    socket_path = tmp_path / "admission.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(8)
    owner_pid = os.getpid()
    owner_start = operator._proc_start(owner_pid)
    actor_pid: int | None = None
    seen: list[tuple[dict, int]] = []
    replies = [
        _admission_reply(
            owner_pid=owner_pid,
            owner_start=owner_start,
            generation=31,
            reservation_id="measure-31",
            kind="measure",
            state="prepared",
            handshake_state="construction_prepared",
            history=["construction_prepared"],
        ),
        _admission_reply(
            owner_pid=owner_pid,
            owner_start=owner_start,
            generation=31,
            reservation_id="measure-31",
            kind="measure",
            state="consumed",
            handshake_state="running",
            history=["construction_prepared", "running"],
            expiry=time.monotonic() + 3130.0,
        ),
        _admission_reply(
            owner_pid=owner_pid,
            owner_start=owner_start,
            generation=31,
            reservation_id="measure-31",
            kind="measure",
            state="consumed",
            handshake_state="finalize_required",
            history=[
                "construction_prepared",
                "running",
                "construction_reset_archived",
                "baseline_bound",
                "construction_released",
                "hold_required",
                "updates_reset_archived",
                "measure_ready",
                "resume_required",
                "resume_ready",
                "finalize_required",
            ],
            expiry=time.monotonic() + 3130.0,
        ),
        _admission_reply(
            owner_pid=owner_pid,
            owner_start=owner_start,
            generation=31,
            reservation_id="measure-31",
            kind="measure",
            state="consumed",
            handshake_state="finalize_required",
            history=[
                "construction_prepared",
                "running",
                "construction_reset_archived",
                "baseline_bound",
                "construction_released",
                "hold_required",
                "updates_reset_archived",
                "measure_ready",
                "resume_required",
                "resume_ready",
                "finalize_required",
            ],
            expiry=time.monotonic() + 3130.0,
        ),
        _admission_reply(
            owner_pid=owner_pid,
            owner_start=owner_start,
            generation=31,
            reservation_id="measure-31",
            kind="measure",
            state="cancelled",
            handshake_state="cancelled",
            history=[
                "construction_prepared",
                "running",
                "construction_reset_archived",
                "baseline_bound",
                "construction_released",
                "hold_required",
                "updates_reset_archived",
                "measure_ready",
                "resume_required",
                "resume_ready",
                "finalize_required",
                "cancelled",
            ],
            expiry=time.monotonic() + 3130.0,
        ),
    ]

    def serve():
        try:
            for response in replies:
                connection, _address = listener.accept()
                with connection:
                    peer = _peer_pid(connection)
                    raw = bytearray()
                    while True:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        raw.extend(chunk)
                    seen.append((json.loads(bytes(raw).decode("utf-8")), peer))
                    connection.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode())
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    actor = operator.CancellationActor(
        socket_path,
        generation=31,
        reservation_id="",
        deadline=time.monotonic() + 20.0,
        owner_pid=owner_pid,
        owner_start=owner_start,
    )
    actor.start()
    actor_pid = actor._process.pid if actor._process is not None else None
    assert actor_pid and actor_pid != owner_pid
    try:
        prepared = actor.call("prepare", generation=31, preflight=False, deadline=time.monotonic() + 10.0)
        assert prepared["reservation"]["reservationId"] == "measure-31"
        actor.call(
            "bootstrap",
            generation=31,
            reservation_id="measure-31",
            expected_constructor_boot="native-construction:boot-31",
            deadline=actor.deadline,
        )
        assert actor.deadline - time.monotonic() > 3000.0
        actor.call("finalize", generation=31, reservation_id="measure-31", deadline=actor.deadline)
        actor.call("recheck", generation=31, reservation_id="measure-31", deadline=actor.deadline)
        cancelled = actor.call("cancel", generation=31, reservation_id="measure-31", deadline=actor.deadline)
        assert cancelled["reservation"]["reservationState"] == "cancelled"
    finally:
        actor.close()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert len(seen) == 5
    assert {peer for _request, peer in seen} == {actor_pid}
    assert all(peer != owner_pid for _request, peer in seen)
    assert [request["command"] for request, _peer in seen] == [
        "native_construction_prepare",
        "native_construction_bootstrap",
        "native_construction_finalize_admit",
        "native_construction_recheck",
        "native_construction_recovery_cancel",
    ]
    print(f"actor_pid={actor_pid} owner_pid={owner_pid} peer_pids={[peer for _request, peer in seen]}")


def test_actor_owns_recovery_begin_and_complete_with_new_canonical_id(tmp_path):
    socket_path = tmp_path / "recovery-admission.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(4)
    owner_pid = os.getpid()
    owner_start = operator._proc_start(owner_pid)
    seen: list[tuple[dict, int]] = []
    preflight = _admission_reply(
        owner_pid=owner_pid,
        owner_start=owner_start,
        generation=31,
        reservation_id="preflight-31",
        kind="preflight",
        state="prepared",
        handshake_state="construction_prepared",
        history=["construction_prepared"],
    )
    recovery_begin = _admission_reply(
        owner_pid=owner_pid,
        owner_start=owner_start,
        generation=32,
        reservation_id="recovery-31",
        kind="recovery",
        state="recovery",
        handshake_state="recovery_required",
        history=["recovery_required"],
        expiry=time.monotonic() + 600.0,
        guardian_generation=5,
    )
    recovery_begin["handshake"]["recoveryKind"] = "restore"
    recovery_complete = _admission_reply(
        owner_pid=owner_pid,
        owner_start=owner_start,
        generation=32,
        reservation_id="recovery-31",
        kind="recovery",
        state="recovered",
        handshake_state="recovered",
        history=["recovery_required", "recovered"],
        expiry=time.monotonic() + 600.0,
        guardian_generation=5,
    )
    recovery_complete["handshake"]["recoveryKind"] = "restore"

    def serve():
        try:
            for response in (preflight, recovery_begin, recovery_complete):
                connection, _address = listener.accept()
                with connection:
                    peer = _peer_pid(connection)
                    raw = bytearray()
                    while True:
                        chunk = connection.recv(4096)
                        if not chunk:
                            break
                        raw.extend(chunk)
                    seen.append((json.loads(bytes(raw).decode("utf-8")), peer))
                    connection.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode())
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    actor = operator.CancellationActor(
        socket_path,
        generation=31,
        reservation_id="",
        deadline=time.monotonic() + 20.0,
        owner_pid=owner_pid,
        owner_start=owner_start,
    )
    actor.start()
    actor_pid = actor._process.pid if actor._process is not None else None
    assert actor_pid and actor_pid != owner_pid
    try:
        actor.call("prepare", generation=31, preflight=True, deadline=time.monotonic() + 10.0)
        completed = actor.call(
            "recovery_begin",
            generation=32,
            reservation_id="",
            deadline=actor.deadline,
        )
        assert completed["reservation"]["reservationId"] == "recovery-31"
        assert actor.reservation_id == "recovery-31"
        terminal = actor.call(
            "recovery_complete",
            generation=32,
            reservation_id="recovery-31",
            deadline=actor.deadline,
        )
        assert terminal["reservation"]["reservationState"] == "recovered"
    finally:
        actor.close()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert len(seen) == 3
    assert {peer for _request, peer in seen} == {actor_pid}
    assert all(peer != owner_pid for _request, peer in seen)
    assert [request["command"] for request, _peer in seen] == [
        "native_construction_control_preflight",
        "native_construction_recovery_begin",
        "native_construction_recovery_complete",
    ]
    assert seen[1][0]["reservationId"] == ""
    print(f"recovery_actor_pid={actor_pid} owner_pid={owner_pid} peer_pids={[peer for _r, peer in seen]}")


def test_native_operator_routes_real_daemon_client_prepare_through_actor(tmp_path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    socket_path = tmp_path / "operator-admission.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(2)
    owner_pid = os.getpid()
    seen: list[tuple[dict, int]] = []
    response = _admission_reply(
        # The daemon's canonical leased owner is distinct from this helper;
        # actor peer ownership is proved independently below.
        owner_pid=4242,
        owner_start="daemon-start-31",
        generation=31,
        reservation_id="operator-measure-31",
        kind="measure",
        state="prepared",
        handshake_state="construction_prepared",
        history=["construction_prepared"],
    )

    def serve():
        try:
            connection, _address = listener.accept()
            with connection:
                peer = _peer_pid(connection)
                raw = bytearray()
                while True:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    raw.extend(chunk)
                seen.append((json.loads(bytes(raw).decode("utf-8")), peer))
                connection.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode())
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    client = operator.DaemonClient(socket_path, timeout=10.0)
    screen = FakeScreen(tmp_path, construct_scene, restore_scene)
    input_device = FakeInput(screen)
    candidate = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_MEASURE_DEFERRED,
        generation=31,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=client,
        screen=screen,
        input_device=input_device,
        receipt_root=tmp_path / "receipts",
    )
    candidate.receipt.begin()
    try:
        binding = candidate._prepare(preflight=False)
        assert binding.reservation_id == "operator-measure-31"
        assert input_device.calls == []
        actor = candidate._cancellation_actor
        assert actor is not None and actor._process is not None
        actor_pid = actor._process.pid
        assert actor_pid != owner_pid
    finally:
        candidate._stop_cancellation_actor()
        input_device.close()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert len(seen) == 1
    assert seen[0][0] == {"command": "native_construction_prepare", "generation": 31}
    assert seen[0][1] == actor_pid
    assert seen[0][1] != owner_pid
    print(f"operator_prepare_actor_pid={actor_pid} owner_pid={owner_pid} peer_pid={seen[0][1]}")


def test_actor_refusal_keeps_admission_actor_armed(tmp_path):
    socket_path = tmp_path / "refusal.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(3)
    owner_pid = os.getpid()
    owner_start = operator._proc_start(owner_pid)
    prepare = _admission_reply(
        owner_pid=owner_pid,
        owner_start=owner_start,
        generation=31,
        reservation_id="refusal-measure-31",
        kind="measure",
        state="prepared",
        handshake_state="construction_prepared",
        history=["construction_prepared"],
    )
    seen: list[dict] = []

    def serve():
        try:
            connection, _address = listener.accept()
            with connection:
                raw = bytearray()
                while chunk := connection.recv(4096):
                    raw.extend(chunk)
                seen.append(json.loads(bytes(raw).decode("utf-8")))
                connection.sendall((json.dumps(prepare) + "\n").encode())
            connection, _address = listener.accept()
            with connection:
                raw = bytearray()
                while chunk := connection.recv(4096):
                    raw.extend(chunk)
                seen.append(json.loads(bytes(raw).decode("utf-8")))
                connection.sendall(b'{"ok":false}\n')
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    actor = operator.CancellationActor(
        socket_path,
        generation=31,
        reservation_id="",
        deadline=time.monotonic() + 20.0,
        owner_pid=owner_pid,
        owner_start=owner_start,
    )
    actor.start()
    try:
        actor.call("prepare", generation=31, preflight=False, deadline=time.monotonic() + 10.0)
        with pytest.raises(operator.OperatorError, match="actor_admission_invalid"):
            actor.call(
                "finalize",
                generation=31,
                reservation_id="refusal-measure-31",
                deadline=actor.deadline,
            )
        assert actor._process is not None and actor._process.is_alive()
    finally:
        actor.close()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert [request["command"] for request in seen] == [
        "native_construction_prepare",
        "native_construction_finalize_admit",
    ]


def test_actor_lost_admission_reply_is_indeterminate_and_remains_armed(tmp_path):
    socket_path = tmp_path / "lost-reply.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(2)
    seen: list[dict] = []

    def serve():
        try:
            connection, _address = listener.accept()
            with connection:
                raw = bytearray()
                while chunk := connection.recv(4096):
                    raw.extend(chunk)
                seen.append(json.loads(bytes(raw).decode("utf-8")))
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    actor = operator.CancellationActor(
        socket_path,
        generation=31,
        reservation_id="",
        deadline=time.monotonic() + 20.0,
        owner_pid=os.getpid(),
        owner_start=operator._proc_start(os.getpid()),
    )
    actor.start()
    try:
        with pytest.raises(operator.AdmissionIndeterminate):
            actor.call("prepare", generation=31, preflight=False, deadline=time.monotonic() + 10.0)
        assert actor._process is not None and actor._process.is_alive()
    finally:
        actor.close()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert seen == [{"command": "native_construction_prepare", "generation": 31}]


def test_actor_admission_survives_owner_death_and_cancels_late_reply(tmp_path):
    socket_path = tmp_path / "held-admission.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(4)
    context = multiprocessing.get_context("spawn")
    status_read, status_write = context.Pipe(duplex=False)
    first_received = threading.Event()
    release_reply = threading.Event()
    seen: list[tuple[dict, int]] = []
    owner_info: dict[str, object] = {}

    def serve():
        try:
            connection, _address = listener.accept()
            with connection:
                peer = _peer_pid(connection)
                raw = bytearray()
                while True:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    raw.extend(chunk)
                request = json.loads(bytes(raw).decode("utf-8"))
                seen.append((request, peer))
                first_received.set()
                assert release_reply.wait(8.0)
                response = _admission_reply(
                    owner_pid=int(owner_info["owner_pid"]),
                    owner_start=str(owner_info["owner_start"]),
                    generation=31,
                    reservation_id="late-measure-31",
                    kind="measure",
                    state="prepared",
                    handshake_state="construction_prepared",
                    history=["construction_prepared"],
                )
                connection.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode())
            connection, _address = listener.accept()
            with connection:
                peer = _peer_pid(connection)
                raw = bytearray()
                while True:
                    chunk = connection.recv(4096)
                    if not chunk:
                        break
                    raw.extend(chunk)
                request = json.loads(bytes(raw).decode("utf-8"))
                seen.append((request, peer))
                response = _admission_reply(
                    owner_pid=int(owner_info["owner_pid"]),
                    owner_start=str(owner_info["owner_start"]),
                    generation=31,
                    reservation_id="late-measure-31",
                    kind="measure",
                    state="cancelled",
                    handshake_state="cancelled",
                    history=["construction_prepared", "cancelled"],
                )
                connection.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode())
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    owner = context.Process(
        target=_actor_admission_owner_fixture,
        args=(str(socket_path), status_write),
        name="native-calibration-actor-admission-owner",
        daemon=False,
    )
    owner.start()
    status_write.close()
    actor_start = None
    try:
        assert status_read.poll(8.0)
        owner_pid, owner_start, actor_pid, actor_start = status_read.recv()
        owner_info.update(owner_pid=owner_pid, owner_start=owner_start)
        assert first_received.wait(8.0)
        assert actor_pid != owner_pid
        os.kill(owner_pid, signal.SIGKILL)
        owner.join(timeout=4.0)
        release_reply.set()
        thread.join(timeout=8.0)
        assert not thread.is_alive()
        assert len(seen) == 2
        assert seen[0][1] == actor_pid and seen[1][1] == actor_pid
        assert all(peer != owner_pid for _request, peer in seen)
        assert seen[0][0]["command"] == "native_construction_prepare"
        assert seen[1][0] == {
            "command": "native_construction_recovery_cancel",
            "generation": 31,
            "reservationId": "late-measure-31",
        }
        print(
            f"late_reply_actor_pid={actor_pid} dead_owner_pid={owner_pid} peers={[peer for _r, peer in seen]}"
        )
    finally:
        release_reply.set()
        _reap_process(owner)
        if "actor_pid" in locals():
            assert _wait_pid_identity_exit(actor_pid, actor_start, 5.0)
        status_read.close()
        thread.join(timeout=3.0)


def test_cancellation_actor_acknowledges_canonical_handoff_without_replacement(tmp_path):
    actor = operator.CancellationActor(
        tmp_path / "unused.sock",
        generation=7,
        reservation_id="reservation-old",
        deadline=time.monotonic() + 8.0,
        owner_pid=os.getpid(),
        owner_start=operator._proc_start(os.getpid()),
    )
    actor.start()
    try:
        actor.update(
            generation=8,
            reservation_id="reservation-new",
            deadline=time.monotonic() + 8.0,
        )
        assert actor.generation == 8
        assert actor.reservation_id == "reservation-new"
    finally:
        actor.close()


def test_operator_handoff_updates_existing_cancellation_actor(tmp_path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    client = FakeClient(7, [])
    client.socket_path = tmp_path / "unused.sock"
    screen = FakeScreen(tmp_path, construct_scene, restore_scene)
    input_device = FakeInput(screen)
    candidate = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=client,
        screen=screen,
        input_device=input_device,
        receipt_root=tmp_path / "receipts",
    )
    candidate.receipt.begin()
    try:
        candidate._prepare(preflight=True)
        actor = candidate._cancellation_actor
        assert actor is not None
        bootstrap_reply = client._reply("running")
        bootstrap_reply["reservation"]["expiresMonotonic"] = actor.deadline + 3130.0
        candidate._adopt_reply(bootstrap_reply, expected_state="running")
        assert actor.deadline == bootstrap_reply["reservation"]["expiresMonotonic"]
        assert candidate._reservation_deadline == bootstrap_reply["reservation"]["expiresMonotonic"]
        recovery = FakeBinding(reservation_id="recovery-1", kind="recovery", state="recovery")
        reply = {
            "ok": True,
            "reservation": recovery.record(7),
            "handshake": {
                "state": "recovery_required",
                "stateHistory": [{"state": "recovery_required", "nativeClock": 1.0}],
            },
        }
        candidate._adopt_handoff(reply, expected_state="recovery_required")
        assert candidate._cancellation_actor is actor
        assert actor.generation == 7
        assert actor.reservation_id == "recovery-1"
    finally:
        candidate._stop_cancellation_actor()
        input_device.close()


def test_handled_signal_handlers_remain_during_cleanup_and_ignore_repeats(tmp_path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    candidate = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=FakeClient(7, []),
        screen=FakeScreen(tmp_path, construct_scene, restore_scene),
        input_device=FakeInput(FakeScreen(tmp_path, construct_scene, restore_scene)),
    )
    candidate._defer_signal_restore = True
    try:
        with pytest.raises(operator.OperatorInterrupted):
            with candidate._signals():
                os.kill(os.getpid(), operator.signal.SIGTERM)
        assert candidate._old_handlers
        candidate._cleanup_started = True
        os.kill(os.getpid(), operator.signal.SIGINT)
        assert candidate._aborted
    finally:
        candidate._defer_signal_restore = False
        candidate._restore_signals()


def test_watchdog_readiness_failure_rolls_back_spawned_process(monkeypatch):
    tool = operator.PinnedInputTool.load()
    real_context = multiprocessing.get_context("spawn")
    spawned = []

    class ContextProxy:
        def Process(self, *args, **kwargs):
            process = real_context.Process(*args, **kwargs)
            spawned.append(process)
            return process

    monkeypatch.setattr(operator.multiprocessing, "get_context", lambda _name: ContextProxy())
    monkeypatch.setattr(
        operator,
        "_read_bounded",
        lambda _fd, _size, *, timeout: (_ for _ in ()).throw(TimeoutError("fixture readiness timeout")),
    )
    try:
        with pytest.raises(operator.OperatorError, match="watchdog readiness"):
            operator.Watchdog(tool, interval=0.02, environment={})
        assert spawned and all(not process.is_alive() for process in spawned)
    finally:
        tool.close()


def test_receipt_publication_failure_is_retained_as_receipt_error(tmp_path, monkeypatch):
    writer = operator.ReceiptWriter(tmp_path / "root", operator.MODE_CONTROL_PREFLIGHT)
    writer.begin()
    monkeypatch.setattr(operator.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(operator.ReceiptError, match="publication"):
        writer.finish("failure", "operator_failure")
    assert writer.attempt is not None
    assert (writer.attempt / ".receipt.json.tmp").exists()


def test_run_receipt_failure_does_not_skip_input_cleanup(tmp_path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, construct_scene, restore_scene)
    input_device = FakeInput(screen)
    client = FakeClient(7, [], kind="preflight")
    input_device.dispatch = client.dispatch
    commands = iter(["/crowley agent defer", "/reload"])
    op = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=client,
        screen=screen,
        input_device=input_device,
        ocr=lambda path: "" if "chat-closed" in path.name else next(commands),
        receipt_root=tmp_path / "receipts",
        sleep=lambda _delay: None,
    )

    def fail_finish(*_args, **_kwargs):
        raise operator.ReceiptError("forced receipt failure")

    op.receipt.finish = fail_finish
    with pytest.raises(operator.ReceiptError, match="forced receipt failure"):
        op.run()
    assert input_device.closed


def test_active_receipt_event_failure_blocks_first_physical_dispatch(tmp_path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, construct_scene, restore_scene)
    input_device = FakeInput(screen)
    op = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=FakeClient(7, [], kind="preflight"),
        screen=screen,
        input_device=input_device,
        receipt_root=tmp_path / "receipts",
    )

    def fail_event(*_args, **_kwargs):
        raise operator.ReceiptError("event publication failed")

    op.receipt.event = fail_event
    with pytest.raises(operator.ReceiptError, match="event publication failed"):
        op.run()
    assert input_device.calls == []
    assert input_device.closed


def test_causal_run_failure_survives_cleanup_close_failure(tmp_path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, construct_scene, restore_scene)

    class CausalClient(FakeClient):
        def prepare(self, generation: int, *, preflight: bool = False):
            raise operator.OperatorError("causal prepare refusal")

    class BrokenInput(FakeInput):
        def close(self):
            self.closed = True
            raise RuntimeError("cleanup close failure")

    input_device = BrokenInput(screen)
    op = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=CausalClient(7, []),
        screen=screen,
        input_device=input_device,
        receipt_root=tmp_path / "receipts",
    )
    with pytest.raises(operator.OperatorError, match="causal prepare refusal"):
        op.run()
    assert input_device.closed
    assert op.receipt.attempt is not None
    receipt = json.loads((op.receipt.attempt / "receipt.json").read_text())
    assert receipt["outcome"] == "failure"


def test_input_action_cleanup_failure_is_named_and_retains_causal_error(tmp_path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, construct_scene, restore_scene)

    class CausalClient(FakeClient):
        def prepare(self, generation: int, *, preflight: bool = False):
            raise operator.OperatorError("causal prepare refusal")

    class BrokenActionInput(FakeInput):
        cleanup_failures = ["pointer.release_now"]

    input_device = BrokenActionInput(screen)
    op = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=CausalClient(7, [], kind="preflight"),
        screen=screen,
        input_device=input_device,
        receipt_root=tmp_path / "receipts",
    )
    with pytest.raises(operator.OperatorError, match="causal prepare refusal"):
        op.run()
    receipt = json.loads((op.receipt.attempt / "receipt.json").read_text())
    events = receipt["events"]
    assert any(
        event["name"] == "mandatory_cleanup_failed" and event["stage"] == "pointer.release_now"
        for event in events
    )
    assert receipt["outcome"] == "failure"


def test_all_fixed_measure_sequence_uses_only_allowlisted_chat_and_ipc(tmp_path):
    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    screen = FakeScreen(tmp_path, construct_scene, restore_scene)
    dual = Image.new("RGB", (1280, 720), (0, 0, 0))
    dual.paste(Image.open(construct_scene).crop((1142, 22, 1146, 26)), (1142, 22))
    dual.paste(Image.open(restore_scene).crop((1211, 22, 1215, 26)), (1211, 22))
    screen.dual = tmp_path / "scene-dual.png"
    dual.save(screen.dual, format="PNG")
    input_device = FakeInput(screen)
    client = FakeClient(7, [])
    input_device.dispatch = client.dispatch
    commands = iter(
        [
            "/crowley agent defer",
            "/reload",
            "/crowley agent release",
            "/crowley agent hold",
            "/crowley agent measure",
            "/crowley agent resume",
            "/crowley agent finalize",
        ]
    )

    def ocr(path):
        if "chat-closed" in path.name:
            return ""
        return next(commands)

    op = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_MEASURE_DEFERRED,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=client,
        screen=screen,
        input_device=input_device,
        ocr=ocr,
        receipt_root=tmp_path / "receipts",
        sleep=lambda _delay: None,
        absence_checker=lambda _path, *, expected_size: True,
    )
    result = op.run()
    assert result["ok"] is True
    typed = [value for kind, value in input_device.calls if kind == "type"]
    assert typed == [
        "/crowley agent defer",
        "/reload",
        "/crowley agent release",
        "/crowley agent hold",
        "/crowley agent measure",
        "/crowley agent resume",
        "/crowley agent finalize",
    ]
    assert all(call.get("command") != "native_bootstrap" for call in client.calls)
    assert any(call.get("command") == "bootstrap" for call in client.calls)
    assert any(call.get("command") == "native_calibrate" for call in client.calls)
    assert any(call.get("command") == "calibration_status" for call in client.calls)


def test_actual_session_ipc_composes_preflight_and_recovery_over_af_unix(tmp_path: Path) -> None:
    """Exercise public native admissions through the real daemon IPC parser."""

    from crowley.session.cli import (
        _BoundedIPCReader,
        _encode_bounded_record,
        _parse_bounded_json,
        _socket_request,
    )
    from crowley.session.controller import ControllerState
    from tests.test_session_native_regressions import _construction_daemon, _construction_frame

    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    identity = replace(identity, fingerprint="a" * 64)
    driver.backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    socket_path = tmp_path / "actual-session.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    listener.listen(8)
    listener.settimeout(0.1)
    stop = threading.Event()
    workers: list[threading.Thread] = []

    begin = _construction_frame(identity, boot="boot-a", sequence=2)
    begin["captureMonotonic"] = 1.0
    ordinary = _construction_frame(identity, boot="ordinary-new", sequence=3)
    ordinary["captureMonotonic"] = 2.0
    observation_count = 0

    def observation_provider() -> dict[str, object]:
        nonlocal observation_count
        observation_count += 1
        return begin if observation_count <= 2 else ordinary

    daemon._native_construction_observation_provider = observation_provider

    from crowley.session.cli import SessionIPC

    ipc = SessionIPC(daemon.controller, owner=daemon)

    def serve_connection(connection: socket.socket) -> None:
        with connection:
            reader = _BoundedIPCReader(connection)
            raw = reader.read_record(4096)
            if raw is None:
                return
            request = _parse_bounded_json(raw)
            assert isinstance(request, dict)
            try:
                response = ipc.dispatch(request)
            except Exception as exc:
                response = {"ok": False, "error": str(exc)}
            try:
                connection.sendall(_encode_bounded_record(response, 524288))
            except OSError:
                # The bounded client may close after its absolute deadline;
                # production `serve` treats that as a transport teardown.
                pass

    def serve() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if stop.is_set():
                    return
                raise
            worker = threading.Thread(target=serve_connection, args=(connection,), daemon=True)
            workers.append(worker)
            worker.start()

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    client = operator.DaemonClient(socket_path, request_fn=_socket_request, timeout=15.0)
    try:
        preflight = _socket_request(
            socket_path,
            {"command": "native_construction_control_preflight", "generation": 7},
            timeout=15.0,
        )
        assert preflight.get("ok") is True, preflight
        # The next public admission is consumed by the closed client facade;
        # no reservation is manufactured by the test.
        assert preflight["ok"] is True
        preflight_id = preflight["reservation"]["reservationId"]
        started = client.recovery_begin(7, preflight_id)
        assert started["ok"] is True
        recovery_id = started["reservation"]["reservationId"]
        assert started["handshake"]["state"] == "recovery_required"
        completed = client.recovery_complete(7, recovery_id)
        assert completed["ok"] is True
        assert completed["handshake"]["state"] == "recovered"
        assert completed["reservation"]["reservationState"] == "recovered"
        assert completed["reservation"]["reservationKind"] == "recovery"
    finally:
        stop.set()
        listener.close()
        server.join(timeout=2.0)
        for worker in workers:
            worker.join(timeout=2.0)
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_control_preflight_operator_uses_actual_daemon_admissions(tmp_path: Path) -> None:
    """Run the control-preflight mode with a production DaemonClient/actor."""

    from crowley.session.cli import (
        SessionIPC,
        _BoundedIPCReader,
        _encode_bounded_record,
        _parse_bounded_json,
        _socket_request,
    )
    from crowley.session.controller import ControllerState
    from tests.test_session_native_regressions import _construction_daemon, _construction_frame

    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    # The production transform searches the reviewed 220x84 top-right panel
    # in a 1920x1080 client capture.  Keep the same sealed template bytes but
    # place the construct role inside that exact region.
    full_construct = Image.new("RGB", (1920, 1080), (0, 0, 0))
    with Image.open(manifest.construct.path) as template:
        # Source-derived Commands.lua geometry at 1920x1080 is construct
        # (1684, 32, 100, 28), restore (1788, 32, 100, 28).
        full_construct.paste(template, (1684, 32))
    full_construct_path = tmp_path / "scene-construct-full.png"
    full_construct.save(full_construct_path, format="PNG")
    full_restore = Image.new("RGB", (1920, 1080), (0, 0, 0))
    with Image.open(manifest.restore.path) as template:
        full_restore.paste(template, (1788, 32))
    full_restore_path = tmp_path / "scene-restore-full.png"
    full_restore.save(full_restore_path, format="PNG")
    construct_scene, restore_scene = full_construct_path, full_restore_path
    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    identity = replace(identity, fingerprint="a" * 64)
    driver.backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    socket_path = tmp_path / "control-operator.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    listener.listen(8)
    listener.settimeout(0.1)
    stop = threading.Event()
    workers: list[threading.Thread] = []
    begin = _construction_frame(identity, boot="boot-a", sequence=2)
    begin["captureMonotonic"] = 1.0
    ordinary = _construction_frame(identity, boot="ordinary-new", sequence=3)
    ordinary["captureMonotonic"] = 2.0
    observations = iter((begin, begin, ordinary))
    daemon._native_construction_observation_provider = lambda: next(observations)
    ipc = SessionIPC(daemon.controller, owner=daemon)

    def serve_connection(connection: socket.socket) -> None:
        with connection:
            reader = _BoundedIPCReader(connection)
            raw = reader.read_record(4096)
            if raw is None:
                return
            request = _parse_bounded_json(raw)
            assert isinstance(request, dict)
            try:
                response = ipc.dispatch(request)
            except Exception as exc:
                response = {"ok": False, "error": str(exc)}
            try:
                connection.sendall(_encode_bounded_record(response, 524288))
            except OSError:
                pass

    def serve() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if stop.is_set():
                    return
                raise
            worker = threading.Thread(target=serve_connection, args=(connection,), daemon=True)
            workers.append(worker)
            worker.start()

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    screen = FakeScreen(tmp_path, construct_scene, restore_scene)
    screen.identity = Identity(
        fingerprint=identity.fingerprint,
        child_geometry=identity.child_geometry,
        target_event_xids=frozenset(identity.target_event_xids),
    )
    input_device = FakeInput(screen)
    commands = iter(("/crowley agent defer", "/reload"))
    client = operator.DaemonClient(socket_path, request_fn=_socket_request, timeout=15.0)
    op = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_CONTROL_PREFLIGHT,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=client,
        screen=screen,
        input_device=input_device,
        ocr=lambda path: "" if "chat-closed" in path.name else next(commands),
        receipt_root=tmp_path / "receipts",
        sleep=lambda _delay: None,
    )
    try:
        result = op.run()
        assert result["ok"] is True
        assert [value for kind, value in input_device.calls if kind == "type"] == [
            "/crowley agent defer",
            "/reload",
        ]
        assert any(kind == "click" and value == "construct" for kind, value in input_device.calls)
    finally:
        stop.set()
        listener.close()
        server.join(timeout=2.0)
        for worker in workers:
            worker.join(timeout=2.0)
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_recover_operator_uses_actual_empty_id_recovery_admission(tmp_path: Path) -> None:
    """Run standalone recovery through a real durable checkpoint and socket."""

    from crowley.session.cli import (
        SessionIPC,
        _BoundedIPCReader,
        _encode_bounded_record,
        _parse_bounded_json,
        _socket_request,
    )
    from crowley.session.controller import ControllerState
    from tests.test_session_native_regressions import _construction_daemon, _construction_frame

    manifest, construct_scene, restore_scene = make_scenes(tmp_path)
    daemon, driver, identity, child = _construction_daemon(tmp_path, state=ControllerState.PAUSED)
    identity = replace(identity, fingerprint="a" * 64)
    driver.backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    daemon.controller.set_native_construction_checkpoint(
        {
            "schema": 1,
            "B0": "boot-a",
            "expectedConstructorBoot": "native-construction:boot-a",
            "ownerGeneration": 7,
            "kind": "measure",
            "phase": "prepared",
        },
        expected_generation=7,
        expected_state=ControllerState.PAUSED,
    )
    begin = _construction_frame(identity, boot="boot-a", sequence=2)
    begin["captureMonotonic"] = 1.0
    ordinary = _construction_frame(identity, boot="ordinary-new", sequence=3)
    ordinary["captureMonotonic"] = 2.0
    observation_count = 0

    def observation_provider() -> dict[str, object]:
        nonlocal observation_count
        observation_count += 1
        return begin if observation_count == 1 else ordinary

    daemon._native_construction_observation_provider = observation_provider
    socket_path = tmp_path / "recover-operator.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    listener.listen(8)
    listener.settimeout(0.1)
    stop = threading.Event()
    workers: list[threading.Thread] = []
    ipc = SessionIPC(daemon.controller, owner=daemon)

    def serve_connection(connection: socket.socket) -> None:
        with connection:
            reader = _BoundedIPCReader(connection)
            raw = reader.read_record(4096)
            if raw is None:
                return
            request = _parse_bounded_json(raw)
            assert isinstance(request, dict)
            try:
                response = ipc.dispatch(request)
            except Exception as exc:
                response = {"ok": False, "error": str(exc)}
            try:
                connection.sendall(_encode_bounded_record(response, 524288))
            except OSError:
                pass

    def serve() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if stop.is_set():
                    return
                raise
            worker = threading.Thread(target=serve_connection, args=(connection,), daemon=True)
            workers.append(worker)
            worker.start()

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    screen = FakeScreen(tmp_path, construct_scene, restore_scene)
    screen.identity = Identity(
        fingerprint=identity.fingerprint,
        child_geometry=identity.child_geometry,
        target_event_xids=frozenset(identity.target_event_xids),
    )
    input_device = FakeInput(screen)
    client = operator.DaemonClient(socket_path, request_fn=_socket_request, timeout=15.0)
    op = operator.NativeCalibrationOperator(
        chat_state=_synthetic_chat_state,
        mode=operator.MODE_RECOVER,
        generation=7,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=client,
        screen=screen,
        input_device=input_device,
        ocr=lambda path: "" if "chat-closed" in path.name else "/reload",
        receipt_root=tmp_path / "receipts",
        sleep=lambda _delay: None,
    )
    try:
        result = op.run()
        assert result["ok"] is True
        assert ("type", "/reload") in input_device.calls
        assert daemon.controller.native_construction_checkpoint() is None
    finally:
        stop.set()
        listener.close()
        server.join(timeout=2.0)
        for worker in workers:
            worker.join(timeout=2.0)
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_resume_required_marks_retained_only_after_valid_callback(tmp_path: Path) -> None:
    from crowley.session.calibration import NativeCalibrationRunner

    capture = tmp_path / "held-100.png"
    capture.write_bytes(b"held-100")
    events: list[str] = []

    class Diagnostic:
        def mark_release(self, path: Path) -> None:
            assert path == capture
            events.append("mark_release")

    class Driver:
        def notify_native_resume_required(self, _identity: object, _boundary: object) -> None:
            events.append("resume_required")

        def notify_native_resume_ready(self, _identity: object, _result: object, _sample: object) -> None:
            events.append("resume_ready")

    runner = NativeCalibrationRunner(Driver(), diagnostic=Diagnostic())
    runner._publish_resume_required_boundary(object(), object(), capture)
    assert events == ["resume_required", "mark_release"]

    rejecting_events: list[str] = []

    class RejectingDriver:
        def notify_native_resume_required(self, _identity: object, _boundary: object) -> None:
            rejecting_events.append("resume_required")
            raise RuntimeError("resume callback rejected")

        def notify_native_resume_ready(self, _identity: object, _result: object, _sample: object) -> None:
            rejecting_events.append("resume_ready")

    rejecting_runner = NativeCalibrationRunner(RejectingDriver(), diagnostic=Diagnostic())
    with pytest.raises(RuntimeError, match="resume callback rejected"):
        rejecting_runner._publish_resume_required_boundary(object(), object(), capture)
    assert rejecting_events == ["resume_required"]
    assert events == ["resume_required", "mark_release"]


# The full composed screen/daemon/agent path is covered by the retained focused IPC and
# native admission tests below; this duplicate giant replay is intentionally retired.


def test_measure_deferred_public_admission_retains_real_failure_status(tmp_path: Path) -> None:
    """Exercise prepare/bootstrap/native-calibrate/status on the actual daemon path."""

    from crowley.session.cli import (
        SessionIPC,
        _BoundedIPCReader,
        _encode_bounded_record,
        _parse_bounded_json,
        _socket_request,
    )
    from tests.test_session_native_regressions import _construction_daemon, _construction_frame

    daemon, driver, identity, child = _construction_daemon(tmp_path)
    identity = replace(identity, fingerprint="a" * 64)
    driver.backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    begin = _construction_frame(identity, boot="boot-a", sequence=2)
    begin["captureMonotonic"] = 1.0
    daemon._native_construction_observation_provider = lambda: begin
    socket_path = tmp_path / "measure-operator.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    listener.listen(8)
    listener.settimeout(0.1)
    stop = threading.Event()
    workers: list[threading.Thread] = []
    ipc = SessionIPC(daemon.controller, owner=daemon)

    def serve_connection(connection: socket.socket) -> None:
        with connection:
            reader = _BoundedIPCReader(connection)
            raw = reader.read_record(4096)
            if raw is None:
                return
            request = _parse_bounded_json(raw)
            assert isinstance(request, dict)
            try:
                response = ipc.dispatch(request)
            except Exception as exc:
                response = {"ok": False, "error": str(exc)}
            try:
                connection.sendall(_encode_bounded_record(response, 524288))
            except OSError:
                pass

    def serve() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if stop.is_set():
                    return
                raise
            worker = threading.Thread(target=serve_connection, args=(connection,), daemon=True)
            workers.append(worker)
            worker.start()

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    client = operator.DaemonClient(socket_path, request_fn=_socket_request, timeout=15.0)
    try:
        prepared = client.prepare(7)
        binding = operator.ReservationBinding.from_record(prepared["reservation"], expected_generation=7)
        bootstrapped = client.bootstrap(7, binding)
        assert bootstrapped["handshake"]["state"] == "running"
        started = client.native_calibrate(7, deadline=time.monotonic() + 8.0)
        status = started["status"]
        assert status["state"] == "NATIVE_CALIBRATION"
        assert status["calibrationOperation"]["kind"] == "native"
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            status = client.calibration_status(deadline=deadline)["status"]
            operation = status["calibrationOperation"]
            if operation["state"] in {"complete", "error"}:
                break
            time.sleep(0.05)
        assert operation["state"] in {"complete", "error"}
        if operation["state"] == "error":
            assert isinstance(operation["error"], str) and operation["error"]
    finally:
        stop.set()
        listener.close()
        server.join(timeout=2.0)
        for worker in workers:
            worker.join(timeout=2.0)
        daemon.request_shutdown()
        driver.close()
        child.terminate()
        child.wait(timeout=3)


def test_observer_manifest_rejects_legacy_schema(tmp_path: Path) -> None:
    observer = tmp_path / "observer"
    observer.write_bytes(b"#!/bin/sh\nexit 0\n")
    observer.chmod(0o700)
    source_root = native_build.NATIVE_ROOT
    source_names = (
        "native/x11_identity_observer/Cargo.toml",
        "native/x11_identity_observer/Cargo.lock",
        "native/x11_identity_observer/src/main.rs",
    )
    source_hashes = {
        name: hashlib.sha256((source_root / name.removeprefix("native/")).read_bytes()).hexdigest()
        for name in source_names
    }
    manifest_path = observer.with_suffix(".manifest.json")
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "crowley.x11-identity-observer.v1",
                "target": "x86_64-unknown-linux-gnu",
                "binary": str(observer),
                "binary_sha256": hashlib.sha256(observer.read_bytes()).hexdigest(),
                "source_sha256": source_hashes,
                "static_inputs": {},
                "toolchain": {},
                "linker": {},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    screen = SimpleNamespace(backend=SimpleNamespace(resolver_kind="pinned-rust-v1"))
    with pytest.raises(operator.OperatorError, match="source manifest schema is malformed"):
        operator._verify_observer_manifest(
            screen, observer, hashlib.sha256(observer.read_bytes()).hexdigest()
        )


@pytest.mark.parametrize("mutation", ["mtime", "path"])
def test_private_json_rejects_eof_or_path_mutation_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """Manifest reads must reject same-size content changes and path swaps."""

    path = tmp_path / "private.json"
    payload = json.dumps({"blob": "x" * 70_000}, separators=(",", ":"))
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o600)
    replacement = tmp_path / "replacement.json"
    replacement.write_text(payload, encoding="utf-8")
    replacement.chmod(0o600)
    real_read = operator.os.read
    mutated = False

    def mutate_after_first_chunk(fd: int, size: int) -> bytes:
        nonlocal mutated
        chunk = real_read(fd, size)
        if chunk and not mutated:
            mutated = True
            if mutation == "mtime":
                before = path.lstat()
                os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
            else:
                os.replace(replacement, path)
        return chunk

    monkeypatch.setattr(operator.os, "read", mutate_after_first_chunk)
    with pytest.raises(operator.OperatorError, match="changed"):
        operator._read_private_json(path, label="private manifest")
    assert mutated


def test_actual_bootstrap_reply_loss_after_prepare_expiry_cancels_known_id(tmp_path: Path) -> None:
    """Keep exact cleanup for a committed bootstrap whose reply misses its short bound."""

    from crowley.session.cli import (
        SessionIPC,
        _BoundedIPCReader,
        _encode_bounded_record,
        _parse_bounded_json,
    )
    from tests.test_session_native_regressions import _construction_daemon, _construction_frame

    daemon, driver, identity, child = _construction_daemon(tmp_path)
    # Keep the real prepare expiry just beyond the five-second cleanup grace
    # so the owner can cross the old bound while the actor remains armed.
    daemon.config = replace(daemon.config, reload_deadline_seconds=6.0)
    identity = replace(identity, fingerprint="a" * 64)
    driver.backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    begin = _construction_frame(identity, boot="boot-a", sequence=2)
    begin["captureMonotonic"] = 1.0
    daemon._native_construction_observation_provider = lambda: begin
    socket_path = tmp_path / "bootstrap-loss.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    listener.listen(8)
    listener.settimeout(0.1)
    stop = threading.Event()
    workers: list[threading.Thread] = []
    seen: list[dict[str, object]] = []
    cancel_records: list[dict[str, object]] = []
    ipc = SessionIPC(daemon.controller, owner=daemon)

    def serve_connection(connection: socket.socket) -> None:
        with connection:
            reader = _BoundedIPCReader(connection)
            raw = reader.read_record(4096)
            if raw is None:
                return
            request = _parse_bounded_json(raw)
            assert isinstance(request, dict)
            seen.append(request)
            response = ipc.dispatch(request)
            if request.get("command") == "native_construction_recovery_cancel":
                cancel_records.append(response)
            if request.get("command") == "native_construction_bootstrap":
                # The daemon has committed and extended the same reservation
                # before this deliberate transport loss.
                time.sleep(0.6)
            try:
                connection.sendall(_encode_bounded_record(response, 524288))
            except OSError:
                pass

    def serve() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if stop.is_set():
                    return
                raise
            worker = threading.Thread(target=serve_connection, args=(connection,), daemon=True)
            workers.append(worker)
            worker.start()

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    actor = operator.CancellationActor(
        socket_path,
        generation=7,
        reservation_id="",
        deadline=time.monotonic() + 20.0,
        owner_pid=os.getpid(),
        owner_start=operator._proc_start(os.getpid()),
    )
    actor.start()
    try:
        prepared = actor.call("prepare", generation=7, preflight=False, deadline=time.monotonic() + 10.0)
        reservation = prepared["reservation"]
        old_expiry = float(reservation["expiresMonotonic"])
        with pytest.raises(operator.AdmissionIndeterminate):
            actor.call(
                "bootstrap",
                generation=7,
                reservation_id=reservation["reservationId"],
                expected_constructor_boot=reservation["expectedConstructorBoot"],
                deadline=time.monotonic() + 0.2,
            )
        remaining = old_expiry - time.monotonic()
        if remaining > 0:
            time.sleep(remaining + 0.2)
        # The private read deadline may expire before the actor serializes its
        # complete indeterminate frame.  In that incomplete-reply case EOF is
        # allowed to cancel the known ID before the old expiry; either outcome
        # is valid only with one exact cancellation and no retry.
        assert actor._process is not None
        if actor._process.is_alive():
            actor.abandon()
            actor._process.join(timeout=4.0)
        assert not actor._process.is_alive()
        assert [request["command"] for request in seen] == [
            "native_construction_prepare",
            "native_construction_bootstrap",
            "native_construction_recovery_cancel",
        ]
    finally:
        # EOF is the owner-death path: the actor must retain its exact known
        # reservation and issue cancellation before it exits.
        actor.abandon()
        if actor._process is not None:
            actor._process.join(timeout=4.0)
            if actor._process.is_alive():
                actor._process.kill()
                actor._process.join(timeout=1.0)
            assert not actor._process.is_alive()
        stop.set()
        listener.close()
        server.join(timeout=3.0)
        for worker in workers:
            worker.join(timeout=3.0)
        driver.close()
        child.terminate()
        child.wait(timeout=3)
    assert [request["command"] for request in seen] == [
        "native_construction_prepare",
        "native_construction_bootstrap",
        "native_construction_recovery_cancel",
    ]
    assert seen[2]["reservationId"] == reservation["reservationId"]
    assert len(cancel_records) == 1
    assert cancel_records[0]["handshake"]["state"] == "cancelled"
    assert cancel_records[0]["reservation"]["reservationId"] == reservation["reservationId"]


def test_complete_bootstrap_server_loss_retains_actor_until_owner_eof(tmp_path: Path) -> None:
    """A complete indeterminate reply keeps exact-ID ownership until EOF."""

    from crowley.session.cli import (
        SessionIPC,
        _BoundedIPCReader,
        _encode_bounded_record,
        _parse_bounded_json,
    )
    from tests.test_session_native_regressions import _construction_daemon, _construction_frame

    daemon, driver, identity, child = _construction_daemon(tmp_path)
    daemon.config = replace(daemon.config, reload_deadline_seconds=6.0)
    identity = replace(identity, fingerprint="a" * 64)
    driver.backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    begin = _construction_frame(identity, boot="boot-a", sequence=2)
    begin["captureMonotonic"] = 1.0
    daemon._native_construction_observation_provider = lambda: begin
    socket_path = tmp_path / "bootstrap-complete-loss.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    listener.listen(8)
    listener.settimeout(0.1)
    stop = threading.Event()
    workers: list[threading.Thread] = []
    seen: list[dict[str, object]] = []
    cancel_records: list[dict[str, object]] = []
    ipc = SessionIPC(daemon.controller, owner=daemon)

    def serve_connection(connection: socket.socket) -> None:
        with connection:
            reader = _BoundedIPCReader(connection)
            raw = reader.read_record(4096)
            if raw is None:
                return
            request = _parse_bounded_json(raw)
            assert isinstance(request, dict)
            seen.append(request)
            response = ipc.dispatch(request)
            if request.get("command") == "native_construction_bootstrap":
                # The daemon has committed the extension.  The peer then
                # disappears before delivering the complete response frame.
                return
            if request.get("command") == "native_construction_recovery_cancel":
                cancel_records.append(response)
            try:
                connection.sendall(_encode_bounded_record(response, 524288))
            except OSError:
                pass

    def serve() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if stop.is_set():
                    return
                raise
            worker = threading.Thread(target=serve_connection, args=(connection,), daemon=True)
            workers.append(worker)
            worker.start()

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    actor = operator.CancellationActor(
        socket_path,
        generation=7,
        reservation_id="",
        deadline=time.monotonic() + 20.0,
        owner_pid=os.getpid(),
        owner_start=operator._proc_start(os.getpid()),
    )
    actor.start()
    reservation = None
    try:
        prepared = actor.call("prepare", generation=7, preflight=False, deadline=time.monotonic() + 10.0)
        reservation = prepared["reservation"]
        old_expiry = float(reservation["expiresMonotonic"])
        with pytest.raises(operator.AdmissionIndeterminate):
            actor.call(
                "bootstrap",
                generation=7,
                reservation_id=reservation["reservationId"],
                expected_constructor_boot=reservation["expectedConstructorBoot"],
                deadline=time.monotonic() + 5.0,
            )
        assert actor._channel_poisoned
        assert actor._control_write >= 0
        with pytest.raises(operator.OperatorError, match="unusable"):
            actor.update(
                generation=7,
                reservation_id=reservation["reservationId"],
                deadline=time.monotonic() + 5.0,
            )
        with pytest.raises(operator.OperatorError, match="(?:unusable|poisoned)"):
            actor.call(
                "recheck",
                generation=7,
                reservation_id=reservation["reservationId"],
                deadline=time.monotonic() + 5.0,
            )
        remaining = old_expiry - time.monotonic()
        if remaining > 0:
            time.sleep(remaining + 0.2)
        assert actor._process is not None and actor._process.is_alive()
        assert [request["command"] for request in seen] == [
            "native_construction_prepare",
            "native_construction_bootstrap",
        ]
        actor.close()
        assert actor._process is not None and not actor._process.is_alive()
    finally:
        actor.abandon()
        if actor._process is not None:
            actor._process.join(timeout=4.0)
            if actor._process.is_alive():
                actor._process.kill()
                actor._process.join(timeout=1.0)
            assert not actor._process.is_alive()
        stop.set()
        listener.close()
        server.join(timeout=3.0)
        for worker in workers:
            worker.join(timeout=3.0)
        driver.close()
        child.terminate()
        child.wait(timeout=3)
    assert reservation is not None
    assert [request["command"] for request in seen] == [
        "native_construction_prepare",
        "native_construction_bootstrap",
        "native_construction_recovery_cancel",
    ]
    assert seen[-1]["reservationId"] == reservation["reservationId"]
    assert len(cancel_records) == 1
    assert cancel_records[0]["handshake"]["state"] == "cancelled"
    assert cancel_records[0]["reservation"]["reservationId"] == reservation["reservationId"]


def test_actual_measurement_error_pauses_and_recovers_with_empty_id(tmp_path: Path) -> None:
    """A real terminal native error must hand off through a newer PAUSED generation."""

    from crowley.session.cli import (
        SessionIPC,
        _BoundedIPCReader,
        _encode_bounded_record,
        _parse_bounded_json,
        _socket_request,
    )
    from tests.test_session_native_regressions import _construction_daemon, _construction_frame

    daemon, driver, identity, child = _construction_daemon(tmp_path)
    identity = replace(identity, fingerprint="a" * 64)
    driver.backend.resolve_identity = lambda **_kwargs: identity  # type: ignore[method-assign]
    begin = _construction_frame(identity, boot="boot-a", sequence=2)
    begin["captureMonotonic"] = 1.0
    ordinary = _construction_frame(identity, boot="ordinary-new", sequence=3)
    ordinary["captureMonotonic"] = 2.0
    observation_count = 0

    def observation_provider() -> dict[str, object]:
        nonlocal observation_count
        observation_count += 1
        return begin if observation_count <= 2 else ordinary

    daemon._native_construction_observation_provider = observation_provider

    def fail_native_meter() -> None:
        raise RuntimeError("fixture native measurement failure")

    # This injects the real daemon's owned measurement boundary failure.  The
    # public prepare/bootstrap/calibrate/status/pause/recovery calls remain
    # production dispatches and must retain the causal error.
    daemon._ensure_native_memory_meter = fail_native_meter  # type: ignore[method-assign]
    socket_path = tmp_path / "terminal-error.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    os.chmod(socket_path, 0o600)
    listener.listen(8)
    listener.settimeout(0.1)
    stop = threading.Event()
    workers: list[threading.Thread] = []
    ipc = SessionIPC(daemon.controller, owner=daemon)

    def serve_connection(connection: socket.socket) -> None:
        with connection:
            reader = _BoundedIPCReader(connection)
            raw = reader.read_record(4096)
            if raw is None:
                return
            request = _parse_bounded_json(raw)
            assert isinstance(request, dict)
            try:
                response = ipc.dispatch(request)
            except Exception as exc:
                response = {"ok": False, "error": str(exc)}
            try:
                connection.sendall(_encode_bounded_record(response, 524288))
            except OSError:
                pass

    def serve() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if stop.is_set():
                    return
                raise
            worker = threading.Thread(target=serve_connection, args=(connection,), daemon=True)
            workers.append(worker)
            worker.start()

    server = threading.Thread(target=serve, daemon=True)
    server.start()
    client = operator.DaemonClient(socket_path, request_fn=_socket_request, timeout=15.0)
    try:
        prepared = client.prepare(7)
        binding = operator.ReservationBinding.from_record(prepared["reservation"], expected_generation=7)
        bootstrapped = client.bootstrap(7, binding)
        assert bootstrapped["handshake"]["state"] == "running"
        started = client.native_calibrate(7, deadline=time.monotonic() + 8.0)
        assert started["status"]["state"] == "NATIVE_CALIBRATION"
        operation = started["status"]["calibrationOperation"]
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            status = client.calibration_status(deadline=deadline)["status"]
            operation = status["calibrationOperation"]
            if operation["state"] in {"complete", "error"}:
                break
            time.sleep(0.02)
        assert operation["state"] == "error"
        assert operation["error"] == "fixture native measurement failure"

        paused = client.pause(7, deadline=time.monotonic() + 8.0)
        paused_status = paused["status"]
        assert paused_status["state"] == "PAUSED"
        assert paused_status["generation"] > 7
        new_generation = paused_status["generation"]
        recovery = client.recovery_begin(new_generation, "")
        assert recovery["handshake"]["state"] == "recovery_required"
        recovery_binding = recovery["reservation"]
        assert recovery_binding["reservationKind"] == "recovery"
        assert recovery_binding["reservationId"] != binding.reservation_id
        terminal = client.recovery_complete(new_generation, recovery_binding["reservationId"])
        assert terminal["handshake"]["state"] == "recovered"
        assert terminal["reservation"]["reservationState"] == "recovered"
    finally:
        stop.set()
        listener.close()
        server.join(timeout=2.0)
        for worker in workers:
            worker.join(timeout=2.0)
        daemon.request_shutdown()
        driver.close()
        child.terminate()
        child.wait(timeout=3)
