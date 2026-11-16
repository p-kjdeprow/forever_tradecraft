from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from crowley_automation import desktop_cli
from crowley_automation.config import AutomationConfig
from crowley_automation.desktop_observe import WindowState
from crowley_automation.ledger import Ledger
from crowley_automation.model import Batch, CollectMail
from crowley_common.frames import pack_cells
from PIL import Image

from tests.automation.native_harness import NativeHarness

DISPLAY = (1920, 1080)
CROP = (352, 48, 256, 256)


def _screen(frame: bytes) -> Image.Image:
    crop = Image.new("RGB", (256, 256), (0, 0, 0))
    pixels = crop.load()
    assert pixels is not None
    for row, bits in enumerate(pack_cells(frame)):
        for column, bit in enumerate(bits):
            color = (255, 255, 255) if bit else (0, 0, 0)
            for dy in range(2):
                for dx in range(2):
                    pixels[column * 2 + dx, row * 2 + dy] = color
    screen = Image.new("RGB", DISPLAY, (17, 17, 17))
    screen.paste(crop, (CROP[0], CROP[1]))
    return screen


def _config(tmp_path: Path) -> AutomationConfig:
    return AutomationConfig(
        wine_prefix=str(tmp_path / "wine"),
        game_root=str(tmp_path / "game"),
        display=":0",
        window_title="World of Warcraft",
        game_executable="Wow.exe",
        game_argv0="C:\\wow.exe",
        window_class="wow.exe",
        character="Tester",
        realm="Normal",
        faction="Alliance",
        house="us-normal-alliance",
        build="1.60.1.70938",
        interface=16001,
        native_region=110,
        canvas_crop=CROP,
    )


def _pin_probe(
    tmp_path: Path, monkeypatch: Any, screen: Image.Image, config: AutomationConfig | None = None
) -> None:
    selected = _config(tmp_path) if config is None else config

    class Process:
        def __init__(self, _matcher: object) -> None: ...

        def pids(self) -> list[int]:
            return [17]

    class Window:
        def __init__(self, *_args: object) -> None: ...

        def observe(self, _pids: list[int]) -> WindowState:
            return WindowState(42, 1, True, True, DISPLAY)

    class Canvas:
        def __init__(self, *_args: object) -> None: ...

        def capture(self, _display: tuple[int, int]) -> Image.Image:
            return screen

    monkeypatch.setattr(desktop_cli, "load_config", lambda: selected)
    monkeypatch.setattr(desktop_cli, "x11_environment", lambda _display: {})
    monkeypatch.setattr(desktop_cli, "ProcProcessSource", Process)
    monkeypatch.setattr(desktop_cli, "X11Window", Window)
    monkeypatch.setattr(desktop_cli, "ScreenCanvas", Canvas)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


def test_probe_rejects_a_decodable_but_stale_automation_canvas(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    client = NativeHarness()
    client.state.now = int(time.time()) - 10
    client.fire("PLAYER_ENTERING_WORLD")
    _pin_probe(tmp_path, monkeypatch, _screen(client.frame_bytes))

    assert desktop_cli.main(["probe-frame"]) == desktop_cli.PROBE_ERROR
    assert "stale or future-dated" in capsys.readouterr().err


def test_probe_rejects_a_frame_that_ages_during_capture(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    capture_start = int(time.time())
    client = NativeHarness()
    client.state.now = capture_start
    client.fire("PLAYER_ENTERING_WORLD")
    _pin_probe(tmp_path, monkeypatch, _screen(client.frame_bytes))
    capture_times = iter((float(capture_start + 10),))
    monkeypatch.setattr(desktop_cli.time, "time", lambda: next(capture_times))

    assert desktop_cli.main(["probe-frame"]) == desktop_cli.PROBE_ERROR
    assert "stale or future-dated" in capsys.readouterr().err


def test_probe_treats_a_canvas_without_the_frame_signature_as_absent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _pin_probe(tmp_path, monkeypatch, Image.new("RGB", DISPLAY, (17, 17, 17)))

    assert desktop_cli.main(["probe-frame"]) == desktop_cli.PROBE_NO_FRAME


def test_probe_rejects_a_corrupt_frame_with_a_valid_signature(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    client = NativeHarness()
    client.fire("PLAYER_ENTERING_WORLD")
    corrupted = bytearray(client.frame_bytes)
    corrupted[26] ^= 1
    _pin_probe(tmp_path, monkeypatch, _screen(bytes(corrupted)))

    assert desktop_cli.main(["probe-frame"]) == desktop_cli.PROBE_ERROR
    assert "CRC mismatch" in capsys.readouterr().err


def test_presence_input_gate_blocks_uncertain_ledger_but_missing_ledger_stays_clear(
    tmp_path: Path, monkeypatch: Any
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setattr(desktop_cli, "presence_ok", lambda: 0)
    assert desktop_cli.main(["presence-ok", "--for-input"]) == 0

    path = state / "crowley" / "automation" / "ledger.sqlite3"
    ledger = Ledger(path)
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

    assert desktop_cli.main(["presence-ok"]) == 0
    assert desktop_cli.main(["presence-ok", "--for-input"]) == 1


def test_presence_input_gate_blocks_an_inflight_runner(tmp_path: Path, monkeypatch: Any) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setattr(desktop_cli, "presence_ok", lambda: 0)
    ledger = Ledger(state / "crowley" / "automation" / "ledger.sqlite3")

    with ledger.runner_lock():
        assert desktop_cli.main(["presence-ok", "--for-input"]) == 1
