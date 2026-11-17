from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from crowley_automation import desktop_cli, desktop_observe
from crowley_automation.config import AutomationConfig
from crowley_automation.desktop_observe import WindowState
from crowley_automation.desktop_session import DesktopSession
from crowley_automation.gui.process import ProcessIdentity
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


def _native_screen(wall_time: int, *, character: str = "Tester", corrupt: bool = False) -> Image.Image:
    client = NativeHarness()
    client.state.now = wall_time
    client.state.character = character
    client.fire("PLAYER_ENTERING_WORLD")
    frame = client.frame_bytes
    if corrupt:
        damaged = bytearray(frame)
        damaged[26] ^= 1
        frame = bytes(damaged)
    return _screen(frame)


def _config(tmp_path: Path) -> AutomationConfig:
    return AutomationConfig(
        wine_prefix=str(tmp_path / "wine"),
        game_root=str(tmp_path / "game"),
        display=":0",
        window_title="World of Warcraft",
        game_executable="Wow.exe",
        game_argv0="C:\\wow.exe",
        window_class="wow.exe",
        product="wowf",
        product_dir="_forever_",
        character="Tester",
        realm="Normal",
        character_select_name="Tester",
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

    class Matcher:
        def identities(self) -> tuple[ProcessIdentity, ...]:
            return (ProcessIdentity(17, 99, 1000, selected.game_argv0, selected.wine_prefix),)

    class MatcherFactory:
        @staticmethod
        def for_game(**_kwargs: str) -> Matcher:
            return Matcher()

    class Window:
        def __init__(self, *_args: object) -> None: ...

        def observe(self, _pids: list[int]) -> WindowState:
            return WindowState(42, 1, True, True, DISPLAY, fullscreen=True)

    class Canvas:
        def __init__(self, *_args: object) -> None: ...

        def capture(self, _display: tuple[int, int]) -> Image.Image:
            return screen

    monkeypatch.setattr(desktop_cli, "load_config", lambda: selected)
    monkeypatch.setattr(desktop_observe, "x11_environment", lambda _display: {})
    monkeypatch.setattr(desktop_observe, "OwnedGameMatcher", MatcherFactory)
    monkeypatch.setattr(desktop_observe, "X11Window", Window)
    monkeypatch.setattr(desktop_observe, "ScreenCanvas", Canvas)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


def _pin_observation_sources(
    tmp_path: Path,
    monkeypatch: Any,
    screens: list[Image.Image],
    *,
    on_capture: Any = None,
) -> tuple[AutomationConfig, list[Image.Image], list[ProcessIdentity], list[WindowState]]:
    selected = _config(tmp_path)
    identity = ProcessIdentity(17, 99, 1000, selected.game_argv0, selected.wine_prefix)
    identities = [identity]
    windows = [WindowState(42, 1, True, True, DISPLAY, fullscreen=True)]
    captures: list[Image.Image] = []

    class Matcher:
        def identities(self) -> tuple[ProcessIdentity, ...]:
            return (identities[0],)

    class MatcherFactory:
        @staticmethod
        def for_game(**_kwargs: str) -> Matcher:
            return Matcher()

    class Window:
        def __init__(self, *_args: object) -> None: ...

        def observe(self, _pids: list[int], *, include_hidden: bool = False) -> WindowState:
            assert not include_hidden
            return windows[0]

    class Canvas:
        def __init__(self, *_args: object) -> None: ...

        def capture(self, _display: tuple[int, int]) -> Image.Image:
            index = len(captures)
            if index >= len(screens):
                raise AssertionError("unexpected additional screenshot")
            captures.append(screens[index])
            if on_capture is not None:
                on_capture(index, identities, windows)
            return screens[index]

    monkeypatch.setattr(desktop_observe, "x11_environment", lambda _display: {})
    monkeypatch.setattr(desktop_observe, "OwnedGameMatcher", MatcherFactory)
    monkeypatch.setattr(desktop_observe, "X11Window", Window)
    monkeypatch.setattr(desktop_observe, "ScreenCanvas", Canvas)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return selected, captures, identities, windows


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
    observe = desktop_cli.observe_native_world
    monkeypatch.setattr(
        desktop_cli,
        "observe_native_world",
        lambda config, **kwargs: observe(config, wall_clock=lambda: float(capture_start + 10), **kwargs),
    )

    assert desktop_cli.main(["probe-frame"]) == desktop_cli.PROBE_ERROR
    assert "stale or future-dated" in capsys.readouterr().err


def test_probe_treats_a_canvas_without_the_frame_signature_as_absent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _pin_probe(tmp_path, monkeypatch, Image.new("RGB", DISPLAY, (17, 17, 17)))

    assert desktop_cli.main(["probe-frame"]) == desktop_cli.PROBE_NO_FRAME


def test_native_observation_timestamp_is_capture_start_not_capture_completion(
    tmp_path: Path, monkeypatch: Any
) -> None:
    selected = _config(tmp_path)
    identity = ProcessIdentity(17, 99, 1000, selected.game_argv0, selected.wine_prefix)
    clocks = {"monotonic": 0.0, "wall": 100.0}

    class Matcher:
        def identities(self) -> tuple[ProcessIdentity, ...]:
            return (identity,)

    class MatcherFactory:
        @staticmethod
        def for_game(**_kwargs: str) -> Matcher:
            return Matcher()

    class Window:
        def __init__(self, *_args: object) -> None: ...

        def observe(self, _pids: list[int], *, include_hidden: bool = False) -> WindowState:
            assert not include_hidden
            return WindowState(42, 1, True, True, DISPLAY, fullscreen=True)

    class Canvas:
        def __init__(self, *_args: object) -> None: ...

        def capture(self, _display: tuple[int, int]) -> Image.Image:
            clocks["monotonic"] += 1.0
            clocks["wall"] += 1.0
            return Image.new("RGB", DISPLAY, (17, 17, 17))

    monkeypatch.setattr(desktop_observe, "x11_environment", lambda _display: {})
    monkeypatch.setattr(desktop_observe, "OwnedGameMatcher", MatcherFactory)
    monkeypatch.setattr(desktop_observe, "X11Window", Window)
    monkeypatch.setattr(desktop_observe, "ScreenCanvas", Canvas)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    observation = desktop_observe.observe_native_world(
        selected,
        clock=lambda: clocks["monotonic"],
        wall_clock=lambda: clocks["wall"],
    )
    assert observation.observed_wall == 100.0
    assert clocks["wall"] == 101.0


def test_stale_frame_gets_one_new_capture_and_returns_that_captures_original_start(
    tmp_path: Path, monkeypatch: Any
) -> None:
    selected, captures, _identities, _windows = _pin_observation_sources(
        tmp_path,
        monkeypatch,
        [_native_screen(995), _native_screen(1002)],
    )
    wall_times = iter([1000.0, 1001.2, 1001.2, 1002.4])
    decode_starts: list[float] = []
    decode = desktop_observe.PixelDecoder.decode

    def track_decode(self: Any, canvas: Any, *, monotonic: float, window_identity: str) -> Any:
        decode_starts.append(monotonic)
        return decode(self, canvas, monotonic=monotonic, window_identity=window_identity)

    monkeypatch.setattr(desktop_observe.PixelDecoder, "decode", track_decode)
    observation = desktop_observe.observe_native_world(
        selected,
        clock=lambda: 0.5 * len(captures),
        wall_clock=lambda: next(wall_times),
    )

    assert len(captures) == 2
    assert observation.snapshot is not None
    assert observation.snapshot["wallTime"] == 1002
    assert observation.observed_wall == 1001.2
    assert decode_starts == [0.0, 0.5]


def test_two_stale_frames_are_rejected_with_bounded_freshness_diagnostics(
    tmp_path: Path, monkeypatch: Any
) -> None:
    selected, captures, _identities, _windows = _pin_observation_sources(
        tmp_path,
        monkeypatch,
        [_native_screen(995), _native_screen(997)],
    )
    wall_times = iter([1000.0, 1001.0, 1001.0, 1002.0])

    with pytest.raises(desktop_observe.ObserveError) as error:
        desktop_observe.observe_native_world(
            selected,
            clock=lambda: 0.5 * len(captures),
            wall_clock=lambda: next(wall_times),
        )

    assert len(captures) == 2
    assert str(error.value).startswith("automation publication is stale or future-dated")
    assert "age=" in str(error.value) and "capture=" in str(error.value)


def test_future_frame_diagnostic_clamps_numeric_values(tmp_path: Path, monkeypatch: Any) -> None:
    selected, captures, _identities, _windows = _pin_observation_sources(
        tmp_path, monkeypatch, [_native_screen(1_000_000_000), _native_screen(1002)]
    )
    wall_times = iter([1000.0, 1001.0])

    with pytest.raises(desktop_observe.ObserveError) as error:
        desktop_observe.observe_native_world(
            selected,
            clock=lambda: 0.5 * len(captures),
            wall_clock=lambda: next(wall_times),
        )

    assert len(captures) == 1
    assert str(error.value).endswith("age=-999.999s, capture=1.000s)")
    assert len(str(error.value)) < 160


@pytest.mark.parametrize(
    ("screen", "message"),
    [
        (_native_screen(1005), "stale or future-dated"),
        (_native_screen(1001, corrupt=True), "automation canvas frame is corrupt"),
        (_native_screen(1001, character="Other"), "publication identity does not match configuration"),
    ],
)
def test_future_corrupt_or_wrong_context_frame_does_not_retry(
    tmp_path: Path, monkeypatch: Any, screen: Image.Image, message: str
) -> None:
    selected, captures, _identities, _windows = _pin_observation_sources(
        tmp_path, monkeypatch, [screen, _native_screen(1002)]
    )
    wall_times = iter([1000.0, 1001.0])

    with pytest.raises(desktop_observe.ObserveError, match=message):
        desktop_observe.observe_native_world(
            selected,
            clock=lambda: 0.5 * len(captures),
            wall_clock=lambda: next(wall_times),
        )

    assert len(captures) == 1


def test_missing_frame_signature_stays_absent_without_a_second_capture(
    tmp_path: Path, monkeypatch: Any
) -> None:
    selected, captures, _identities, _windows = _pin_observation_sources(
        tmp_path,
        monkeypatch,
        [Image.new("RGB", DISPLAY, (17, 17, 17)), _native_screen(1002)],
    )
    wall_times = iter([1000.0, 1001.0])
    observation = desktop_observe.observe_native_world(
        selected,
        clock=lambda: 0.5 * len(captures),
        wall_clock=lambda: next(wall_times),
    )

    assert len(captures) == 1
    assert observation.snapshot is None
    assert observation.observed_wall == 1000.0


@pytest.mark.parametrize("drift", ["process", "window", "session"])
def test_stale_frame_retry_rejects_process_window_or_session_drift(
    tmp_path: Path, monkeypatch: Any, drift: str
) -> None:
    def drift_after_capture(
        index: int, identities: list[ProcessIdentity], windows: list[WindowState]
    ) -> None:
        if index != 0:
            return
        if drift == "process":
            original = identities[0]
            identities[0] = ProcessIdentity(
                original.pid, original.start_ticks + 1, original.uid, original.argv0, original.wine_prefix
            )
        elif drift == "window":
            windows[0] = WindowState(43, 1, True, True, DISPLAY, fullscreen=True)

    selected, captures, _identities, _windows = _pin_observation_sources(
        tmp_path,
        monkeypatch,
        [_native_screen(995), _native_screen(1002)],
        on_capture=drift_after_capture,
    )
    wall_times = iter([1000.0, 1001.0])

    class SessionProvider:
        calls = 0

        def current(self) -> DesktopSession:
            self.calls += 1
            fingerprint = "initial" if self.calls == 1 else "changed"
            return DesktopSession({}, fingerprint)

    session_provider = SessionProvider()
    options: dict[str, Any] = {
        "clock": lambda: 0.5 * len(captures),
        "wall_clock": lambda: next(wall_times),
    }
    if drift == "session":
        options["session_provider"] = session_provider

    with pytest.raises(desktop_observe.ObserveError) as error:
        desktop_observe.observe_native_world(selected, **options)

    assert len(captures) == 1
    if drift == "session":
        assert str(error.value) == "desktop session changed during observation"
    else:
        assert str(error.value) == "process, window, or original capture changed during observation"


def test_fresh_second_frame_does_not_reset_original_observation_deadline(
    tmp_path: Path, monkeypatch: Any
) -> None:
    selected, captures, _identities, _windows = _pin_observation_sources(
        tmp_path,
        monkeypatch,
        [_native_screen(995), _native_screen(1002)],
    )
    wall_times = iter([1000.0, 1001.0, 1001.0, 1002.0])

    with pytest.raises(desktop_observe.ObserveError) as error:
        desktop_observe.observe_native_world(
            selected,
            clock=lambda: 1.6 * len(captures),
            wall_clock=lambda: next(wall_times),
        )

    assert len(captures) == 2
    assert str(error.value) == "process, window, or original capture changed during observation"


def test_probe_rejects_a_corrupt_frame_with_a_valid_signature(
    tmp_path: Path, monkeypatch: Any, capsys: Any
) -> None:
    client = NativeHarness()
    client.fire("PLAYER_ENTERING_WORLD")
    corrupted = bytearray(client.frame_bytes)
    corrupted[26] ^= 1
    _pin_probe(tmp_path, monkeypatch, _screen(bytes(corrupted)))

    assert desktop_cli.main(["probe-frame"]) == desktop_cli.PROBE_ERROR
    assert "automation canvas frame is corrupt" in capsys.readouterr().err


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
