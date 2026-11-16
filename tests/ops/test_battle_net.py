"""Battle.net Play button locator tests on synthetic screenshots."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from crowley_automation import battle_net
from crowley_automation.battle_net import BattleNetPlayError, Box, inner_target, locate_play_button
from crowley_automation.desktop_input import ActError, GuiLease, gui_lock_path
from crowley_automation.ledger import Ledger
from crowley_automation.model import Batch, CollectMail
from PIL import Image, ImageDraw, ImageFont

PLAY_BLUE = (0, 116, 224)
WINDOW = Box(154, 70, 1664, 940)


def _screen() -> Image.Image:
    return Image.new("RGB", (1920, 1080), (20, 20, 24))


def _rect(
    image: Image.Image,
    left: int,
    top: int,
    width: int,
    height: int,
    color=PLAY_BLUE,
    label: str | None = "Play",
) -> None:
    draw = ImageDraw.Draw(image)
    draw.rectangle((left, top, left + width - 1, top + height - 1), fill=color)
    if label:
        font = ImageFont.load_default(size=26)
        draw.text((left + width / 2, top + height / 2), label, fill=(255, 255, 255), font=font, anchor="mm")


def test_locates_play_button_beside_gear_button() -> None:
    image = _screen()
    _rect(image, 192, 942, 230, 55)
    _rect(image, 426, 942, 40, 55, label=None)  # gear button across a dark gap
    assert locate_play_button(image, WINDOW) == Box(192, 942, 230, 55)


def test_inner_target_is_the_centred_sixty_percent() -> None:
    inner = inner_target(Box(192, 942, 230, 55))
    assert (inner.width, inner.height) == (138, 33)
    assert inner.left == 192 + (230 - 138) // 2
    assert inner.top == 942 + (55 - 33) // 2


def test_missing_button_fails() -> None:
    with pytest.raises(BattleNetPlayError, match="no blue Play button"):
        locate_play_button(_screen(), WINDOW)


def test_blue_outside_the_bottom_left_quarter_is_ignored() -> None:
    image = _screen()
    _rect(image, 1000, 942, 230, 55)
    _rect(image, 192, 300, 230, 55)
    with pytest.raises(BattleNetPlayError, match="no blue Play button"):
        locate_play_button(image, WINDOW)


def test_another_blue_button_without_the_play_label_is_ignored() -> None:
    # Live 2026-11-09: a second plausible blue region made a relaunch fail.
    image = _screen()
    _rect(image, 192, 942, 230, 55)
    _rect(image, 192, 700, 230, 55, label="Play Now")
    assert locate_play_button(image, WINDOW) == Box(192, 942, 230, 55)


def test_two_plausible_buttons_are_ambiguous() -> None:
    image = _screen()
    _rect(image, 192, 942, 230, 55)
    _rect(image, 192, 700, 230, 55)
    with pytest.raises(BattleNetPlayError, match="ambiguous"):
        locate_play_button(image, WINDOW)


@pytest.mark.parametrize("size", [(100, 55), (230, 20), (500, 55), (230, 200)])
def test_implausible_size_fails(size: tuple[int, int]) -> None:
    image = _screen()
    _rect(image, 192, 800, *size)
    with pytest.raises(BattleNetPlayError, match="implausible"):
        locate_play_button(image, WINDOW)


def test_tiny_blue_specks_are_not_candidates() -> None:
    image = _screen()
    _rect(image, 192, 942, 230, 55)
    _rect(image, 400, 700, 5, 5, label=None)
    assert locate_play_button(image, WINDOW) == Box(192, 942, 230, 55)


def test_cli_wires_the_command(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from pathlib import Path

    from crowley_automation.desktop_cli import main

    seen: list[int | None] = []
    monkeypatch.setattr(battle_net, "click_play", lambda window: seen.append(window) or Path("evidence.json"))
    assert main(["play", "--window", "42"]) == 0
    assert seen == [42]

    def fail(_window: int | None) -> Path:
        raise BattleNetPlayError("no blue Play button region found in the Battle.net window")

    monkeypatch.setattr(battle_net, "click_play", fail)
    assert main(["play"]) == 1
    assert "no blue Play button" in capsys.readouterr().err


def test_hover_blue_is_still_located() -> None:
    image = _screen()
    _rect(image, 192, 942, 230, 55, color=(48, 160, 255))
    assert locate_play_button(image, WINDOW) == Box(192, 942, 230, 55)


@pytest.mark.parametrize("label", ["Update", "Install", "Playing", None])
def test_other_labels_are_not_play(label: str | None) -> None:
    image = _screen()
    _rect(image, 192, 942, 230, 55, label=label)
    with pytest.raises(BattleNetPlayError, match="glyphs|label"):
        locate_play_button(image, WINDOW)


def test_off_centre_play_label_fails() -> None:
    image = _screen()
    _rect(image, 192, 942, 230, 55, label=None)
    ImageDraw.Draw(image).text(
        (210, 970), "Play", fill=(255, 255, 255), font=ImageFont.load_default(size=26), anchor="lm"
    )
    with pytest.raises(BattleNetPlayError, match="centred"):
        locate_play_button(image, WINDOW)


class _FakePermit:
    def __init__(self, log: list[str], fail_press: bool) -> None:
        self.log = log
        self.fail_press = fail_press

    def __enter__(self) -> _FakePermit:
        self.log.append("permit")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.log.append("close")

    def precommit(self) -> int:
        self.log.append("precommit")
        return 111

    def record_press(self) -> int:
        self.log.append("record_press")
        return 222


class _FakeImage:
    fd = 99


class _FakeTool:
    image = _FakeImage()
    proc_path = "/fake"

    def close(self) -> None:
        pass


def _install_fakes(monkeypatch: pytest.MonkeyPatch, tmp_path, *, press_fails: bool) -> list[str]:
    log: list[str] = []
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    for name, value in {
        "XDG_RUNTIME_DIR": str(runtime),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "DISPLAY": ":0",
        "XAUTHORITY": "/unused",
    }.items():
        monkeypatch.setenv(name, value)
    screen = _screen()
    _rect(screen, 192, 942, 230, 55)
    pointer = {"x": 100, "y": 100}

    def capture(_directory, _display):
        log.append("capture")
        return screen

    def tool_run(_tool, args, _timeout):
        args = list(args)
        if args[0] == "mousemove" or "mousemove" in args:
            log.append("motion")
            index = len(args) - 1 - args[::-1].index("mousemove")
            pointer["x"], pointer["y"] = int(args[index + 1]), int(args[index + 2])
        elif args[0] == "mousedown":
            log.append("press")
            if press_fails:
                raise BattleNetPlayError("emitter died")
        elif args == ["mouseup", "1"]:
            log.append("release")
        else:
            raise AssertionError(args)
        return ""

    def position(_tool):
        log.append("pointer")
        return pointer["x"], pointer["y"], 7

    monkeypatch.setattr(battle_net, "window_geometry", lambda _w: WINDOW)
    monkeypatch.setattr(battle_net, "_display_size", lambda: (1920, 1080))
    monkeypatch.setattr(battle_net, "_capture", capture)
    monkeypatch.setattr(battle_net.PinnedInputTool, "load", classmethod(lambda cls: _FakeTool()))
    monkeypatch.setattr(battle_net, "acquire_click_rate_permit", lambda: _FakePermit(log, press_fails))
    monkeypatch.setattr(battle_net, "_tool_run", tool_run)
    monkeypatch.setattr(battle_net, "_pointer_position", position)
    monkeypatch.setattr(battle_net, "_is_window_or_descendant", lambda _x, _a: log.append("window") or True)
    monkeypatch.setattr(
        battle_net,
        "Presence",
        lambda: type("IdlePresence", (), {"human_active": lambda _self: (False, "idle")})(),
    )
    return log


def test_click_play_follows_the_driver_protocol(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    log = _install_fakes(monkeypatch, tmp_path, press_fails=False)
    evidence = battle_net.click_play(window=7)
    assert log == [
        "capture",
        "permit",
        "pointer",  # plan from the pointer read inside the held permit
        "pointer",  # start still matches the planned first sample
        "motion",
        "pointer",  # endpoint check
        "window",
        "precommit",
        "press",
        "record_press",
        "close",
    ]
    assert evidence.stat().st_mode & 0o777 == 0o600
    recorded = battle_net.json.loads(evidence.read_text())
    assert recorded["phase"] == "pressed"
    assert recorded["precommitMonotonicNs"] == 111 and recorded["pressMonotonicNs"] == 222
    assert recorded["start"] == [100, 100]


def test_failure_after_precommit_releases_the_button(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    log = _install_fakes(monkeypatch, tmp_path, press_fails=True)
    with pytest.raises(BattleNetPlayError, match="emitter died"):
        battle_net.click_play(window=7)
    assert log[-4:] == ["precommit", "press", "release", "close"]
    assert "record_press" not in log


def test_direct_play_refuses_competing_gui_lease_before_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    log = _install_fakes(monkeypatch, tmp_path, press_fails=False)
    monkeypatch.setattr(battle_net, "GuiLease", lambda path: GuiLease(path, timeout=0.05))
    with GuiLease(gui_lock_path()).hold(), pytest.raises(BattleNetPlayError, match="GUI lease unavailable"):
        battle_net.click_play(window=7)
    assert log == []


def test_direct_play_refuses_recent_human_input_before_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    log = _install_fakes(monkeypatch, tmp_path, press_fails=False)
    monkeypatch.setattr(
        battle_net,
        "Presence",
        lambda: type("ActivePresence", (), {"human_active": lambda _self: (True, "recent input")})(),
    )
    with pytest.raises(BattleNetPlayError, match="physical input detected"):
        battle_net.click_play(window=7)
    assert log == []


def _default_ledger_path() -> Path:
    return Path(os.environ["XDG_STATE_HOME"]) / "crowley" / "automation" / "ledger.sqlite3"


def test_direct_play_refuses_an_inflight_runner_before_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    log = _install_fakes(monkeypatch, tmp_path, press_fails=False)
    ledger = Ledger(_default_ledger_path())

    with ledger.runner_lock(), pytest.raises(BattleNetPlayError, match="automation_running"):
        battle_net.click_play(window=7)

    assert log == []


def test_direct_play_uses_the_launcher_inherited_gui_lease(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    log = _install_fakes(monkeypatch, tmp_path, press_fails=False)
    parent = GuiLease(gui_lock_path())
    contender = GuiLease(gui_lock_path(), timeout=0.05)

    with parent.hold():
        assert parent.fd is not None
        monkeypatch.setenv("CROWLEY_GUI_LEASE_FD", str(parent.fd))
        evidence = battle_net.click_play(window=7)
        assert evidence.exists()
        assert "press" in log
        with pytest.raises(ActError, match="held elsewhere"), contender.hold():
            pass


def test_direct_play_refuses_an_uncertain_effect_before_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    log = _install_fakes(monkeypatch, tmp_path, press_fails=False)
    ledger = Ledger(_default_ledger_path())
    now = int(time.time())
    batch = Batch("us-normal-alliance", "Tester", now + 300, (CollectMail(2),))
    ledger.prepare(batch, now=now)
    ledger.claim_dispatch(batch.batch_id, 0, 0, now=now, session_boot_id="boot", receipt_baseline=1)
    ledger.finish_step(
        batch.batch_id,
        0,
        0,
        "uncertain",
        now=now,
        callback_delivered=None,
        postcondition=None,
        action_complete=None,
        receipt=None,
    )

    with pytest.raises(BattleNetPlayError, match="automation_uncertain"):
        battle_net.click_play(window=7)

    assert log == []


@pytest.mark.parametrize(
    ("active_at", "motion_expected"),
    [(2, False), (3, True)],
)
def test_play_rechecks_presence_before_motion_and_press(
    monkeypatch: pytest.MonkeyPatch, tmp_path, active_at: int, motion_expected: bool
) -> None:
    log = _install_fakes(monkeypatch, tmp_path, press_fails=False)
    count = 0

    class SequencePresence:
        def human_active(self) -> tuple[bool, str]:
            nonlocal count
            count += 1
            return count == active_at, "recent input"

    monkeypatch.setattr(battle_net, "Presence", SequencePresence)
    with pytest.raises(BattleNetPlayError, match="physical input detected"):
        battle_net.click_play(window=7)
    assert ("motion" in log) is motion_expected
    assert "press" not in log
