"""Bounded, model-only login/recovery adapter.

Normal sweeps never call this module.  It exists for the four explicitly named
character/login observations and cannot emit a command or coordinate.
"""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crowley.config import CODEX_CLI_VERSION, CODEX_MODEL


class ModelError(RuntimeError):
    """The bounded CLI turn was unavailable or returned an unsafe result."""


DECISIONS = {"dismiss_disconnect", "reconnect_game", "select_arthur", "enter_world"}
OBSERVED_STATES = {
    "disconnect_dialog",
    "game_client",
    "character_select",
    "arthur_selected",
    "world",
    "auction_house_open",
    "mailbox_world",
    "executor_mailbox",
}
OBSERVED_CONTROLS = {
    "dismiss_disconnect",
    "reconnect_game",
    "arthur_row",
    "enter_world",
    "auctioneer_buckler",
    "auction_house",
    "world",
    "mailbox_object",
    "crowley_executor_button",
}
CLI_VERSION = CODEX_CLI_VERSION
MAX_USAGE_TOKENS = 2**63 - 1
KNOWN_DIAGNOSTIC = (
    "Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; "
    "enable `features.code_mode_host` and install `codex-code-mode-host`."
)
KNOWN_STDERR_NOTICE = "Reading additional input from stdin...\n"


@dataclass(frozen=True)
class ModelDecision:
    action: str
    target: str


@dataclass(frozen=True)
class ModelObservation:
    state: str
    facts: dict[str, str]
    # Bounds are measured in physical pixels relative to the verified model
    # crop.  ``None`` is retained as a compatibility default for callers that
    # only classify a scene; the production anchor path rejects it before any
    # input can be dispatched.
    anchor: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class ModelRouteObservation:
    scene: str
    route_id: str
    target_visible: bool
    ground_clear: bool


@dataclass(frozen=True)
class ModelViewportObservation:
    """One fresh model-measured point proven clear of UI and unit actors."""

    scene: str
    point: tuple[int, int]
    ui_clear: bool
    units_clear: bool


class LunaAdapter:
    def __init__(
        self,
        *,
        neutral_dir: str | os.PathLike[str],
        timeout: float = 45.0,
        model: str = CODEX_MODEL,
        cli_version: str = CLI_VERSION,
        codex_path: str | os.PathLike[str],
    ):
        neutral_value = os.fspath(neutral_dir)
        if not neutral_value:
            raise ModelError("model neutral directory is required")
        self.neutral_dir = Path(neutral_value)
        self.timeout = timeout
        self.model = model
        self.cli_version = cli_version
        codex_value = os.fspath(codex_path)
        if not codex_value:
            raise ModelError("configured Codex executable path is required")
        self.codex_path = os.path.realpath(codex_value)
        self._cancelled = threading.Event()
        self._process_lock = threading.RLock()
        self._model_call_lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._cancel_epoch = 0
        if self.model != CODEX_MODEL or self.cli_version != CLI_VERSION:
            raise ModelError("configured model or Codex CLI version is outside the pinned boundary")
        if not os.path.isabs(self.codex_path):
            raise ModelError("configured Codex executable must be an absolute path")
        if not os.path.isfile(self.codex_path) or not os.access(self.codex_path, os.X_OK):
            raise ModelError(f"configured Codex executable is not executable: {self.codex_path}")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "XDG_RUNTIME_DIR", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"}
        }
        try:
            version = subprocess.run(
                [self.codex_path, "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ModelError(f"unable to verify Codex CLI at startup: {exc}") from exc
        if version.stdout.strip() != self.cli_version or version.stderr.strip():
            raise ModelError(f"unexpected Codex CLI version: {version.stdout.strip()!r}")

    def cancel(self) -> None:
        """Cancel the active bounded turn and its complete process group."""

        with self._process_lock:
            self._cancel_epoch += 1
            self._cancelled.set()
            process = self._process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # A call that is still inside ``_run_cli`` owns the model-call
            # lock and will perform the bounded reap itself.  A process left
            # live after a prior deadline has no caller left to reap it, so
            # cancellation takes ownership of that cleanup when the lock is
            # available and escalates before returning.
            if self._model_call_lock.acquire(blocking=False):
                try:
                    reaped = self._terminate_process_bounded(process, time.monotonic() + 0.5)
                    if reaped:
                        with self._process_lock:
                            if self._process is process:
                                self._process = None
                finally:
                    self._model_call_lock.release()

    def shutdown(self) -> None:
        """Cancel and boundedly reap any retained model process owner."""

        self.cancel()
        # If a model turn is still running, its cancellation path owns the
        # process.  Wait briefly for that bounded path to release the call
        # lock; a retained process from a completed call is cleaned here.
        if not self._model_call_lock.acquire(timeout=0.75):
            raise ModelError("model shutdown could not acquire the call owner")
        try:
            with self._process_lock:
                process = self._process
            if process is None:
                return
            if process.poll() is None and not self._terminate_process_bounded(
                process, time.monotonic() + 0.5
            ):
                raise ModelError("model shutdown cleanup exceeded its bounded deadline")
            with self._process_lock:
                if self._process is process and process.poll() is not None:
                    self._process = None
        finally:
            self._model_call_lock.release()

    @staticmethod
    def command(
        schema_path: Path,
        image_path: Path,
        neutral_dir: Path,
        *,
        model: str = "gpt-6-luna",
        executable: str = "codex",
        prompt: str = (
            "You are the bounded Crowley login observer. Inspect only the supplied verified "
            "game-client crop. Return exactly one JSON object with action and target. Choose "
            "a named action only when its visible precondition is present; never invent coordinates."
        ),
    ) -> list[str]:
        disabled = [
            "shell_tool",
            "unified_exec",
            "code_mode_host",
            "computer_use",
            "apps",
            "plugins",
            "multi_agent",
            "image_generation",
            "browser_use",
            "browser_use_external",
            "in_app_browser",
            "view_image",
        ]
        args = [
            executable,
            "exec",
            "--model",
            model,
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "-C",
            str(neutral_dir),
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--json",
            "--output-schema",
            str(schema_path),
            "-i",
            str(image_path),
            "-c",
            'model_reasoning_effort="low"',
            "--disable",
            "hooks",
        ]
        for feature in disabled:
            args.extend(("--disable", feature))
        args.append(prompt)
        return args

    def decide(
        self,
        image: bytes,
        *,
        target: str = "game-client",
        verified_crop: tuple[int, int, int, int] | None = None,
        timeout: float | None = None,
    ) -> ModelDecision:
        if not isinstance(target, str) or target != "game-client":
            raise ModelError("only the verified game client crop is supported")
        if (
            verified_crop is None
            or verified_crop[2] < 640
            or verified_crop[3] < 480
            or verified_crop[0] < 0
            or verified_crop[1] < 0
        ):
            raise ModelError("model image lacks a verified client-area crop")
        self.neutral_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.neutral_dir, 0o700)
        with tempfile.TemporaryDirectory(prefix="crowley-model-", dir=self.neutral_dir) as temporary:
            directory = Path(temporary)
            image_path = directory / "screen.png"
            schema_path = directory / "decision.json"
            image_path.write_bytes(image)
            os.chmod(image_path, 0o600)
            schema_path.write_text(self._decision_schema(), encoding="utf-8")
            os.chmod(schema_path, 0o600)
            stdout = self._run_cli(
                schema_path,
                image_path,
                prompt=(
                    "You are the bounded Crowley login observer. Inspect only the supplied "
                    "verified game-client crop. Return exactly one JSON object with action "
                    "and target. Choose a named action only when its visible precondition is "
                    "present; never invent coordinates."
                ),
                timeout=timeout,
            )
            return self._parse(stdout)

    def observe(
        self,
        image: bytes,
        *,
        target: str = "game-client",
        verified_crop: tuple[int, int, int, int] | None = None,
        requested_state: str | None = None,
        requested_control: str | None = None,
        timeout: float | None = None,
    ) -> ModelObservation:
        """Classify one fresh verified client crop into named visual facts.

        The caller still validates the returned facts against the configured
        identity and action contract.  The model reports what is visible in
        this image; the requested action is never used as the observation.
        """

        if not isinstance(target, str) or target != "game-client":
            raise ModelError("only the verified game client crop is supported")
        if requested_state is not None and not isinstance(requested_state, str):
            raise ModelError("requested visual state must be a string")
        if requested_control is not None and not isinstance(requested_control, str):
            raise ModelError("requested visual control must be a string")
        if requested_state is not None and requested_state not in OBSERVED_STATES:
            raise ModelError("requested visual state is outside the named observer boundary")
        if requested_control is not None and requested_control not in OBSERVED_CONTROLS:
            raise ModelError("requested visual control is outside the named observer boundary")
        if (requested_state is None) != (requested_control is None):
            raise ModelError("requested visual state and control must be supplied together")
        if (
            verified_crop is None
            or verified_crop[2] < 640
            or verified_crop[3] < 480
            or verified_crop[0] < 0
            or verified_crop[1] < 0
        ):
            raise ModelError("model image lacks a verified client-area crop")
        self.neutral_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.neutral_dir, 0o700)
        with tempfile.TemporaryDirectory(prefix="crowley-model-", dir=self.neutral_dir) as temporary:
            directory = Path(temporary)
            image_path = directory / "screen.png"
            schema_path = directory / "observation.json"
            image_path.write_bytes(image)
            os.chmod(image_path, 0o600)
            schema_path.write_text(self._observation_schema(), encoding="utf-8")
            os.chmod(schema_path, 0o600)
            stdout = self._run_cli(
                schema_path,
                image_path,
                prompt=(
                    "You are the bounded Crowley visual observer. Inspect only the supplied "
                    "verified game-client crop and report the visible named state, facts, "
                    "and bounds of the requested visible control. "
                    "Return exactly one JSON object. Use only the enum state names and report "
                    "the visible scene, character, realm, modal, code, control, and idle facts. "
                    "The bounds object must be physical pixel bounds relative to this crop, "
                    "with integer x, y, width, and height. The requested control is only a "
                    "search target: report its bounds only when it is visibly present. Never "
                    "infer a state or coordinate from an intended action or process identity; "
                    "if a fact or control is not visible, report an empty string and zero bounds."
                    + (
                        f" Locate control {requested_control!r} in state {requested_state!r}."
                        if requested_control and requested_state
                        else ""
                    )
                ),
                timeout=timeout,
            )
            return self._parse_observation(stdout)

    def observe_route(
        self,
        image: bytes,
        *,
        target: str = "game-client",
        verified_crop: tuple[int, int, int, int] | None = None,
        target_x: float | None = None,
        target_y: float | None = None,
        origin_x: float | None = None,
        origin_y: float | None = None,
        corridor_heading: float | None = None,
        corridor_distance: float | None = None,
        timeout: float | None = None,
    ) -> ModelRouteObservation:
        if (
            target != "game-client"
            or verified_crop is None
            or verified_crop[2] < 640
            or verified_crop[3] < 480
        ):
            raise ModelError("route observation lacks a verified client-area crop")
        has_target = target_x is not None or target_y is not None
        if has_target and (
            target_x is None
            or target_y is None
            or isinstance(target_x, bool)
            or not isinstance(target_x, (int, float))
            or not math.isfinite(float(target_x))
            or isinstance(target_y, bool)
            or not isinstance(target_y, (int, float))
            or not math.isfinite(float(target_y))
        ):
            raise ModelError("route observation requires both finite waypoint coordinates")
        for value, name in (
            (origin_x, "origin_x"),
            (origin_y, "origin_y"),
            (corridor_heading, "corridor_heading"),
            (corridor_distance, "corridor_distance"),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ModelError(f"route observation {name} must be finite")
        if not has_target and (
            corridor_heading is None or corridor_distance is None or float(corridor_distance) <= 0.0
        ):
            raise ModelError("egocentric route observation requires a positive corridor")
        if has_target and corridor_distance is not None and float(corridor_distance) <= 0.0:
            raise ModelError("route observation corridor distance must be positive")
        if has_target:
            route_description = (
                f"the short route to ({float(target_x):.3f}, {float(target_y):.3f}) "
                f"from the measured origin ({float(origin_x if origin_x is not None else target_x):.3f}, "
                f"{float(origin_y if origin_y is not None else target_y):.3f})"
            )
        else:
            route_description = "the short directly egocentric corridor from the measured player origin"
        route_prompt = (
            "You are the bounded Crowley local-route observer. Inspect only the supplied "
            "verified game-client crop. Return whether the visible world scene proves "
            f"{route_description} and clear ground. "
            + (
                "The native frame's measured player origin is "
                f"({float(origin_x if origin_x is not None else target_x):.3f}, "
                f"{float(origin_y if origin_y is not None else target_y):.3f}); "
                if has_target
                else "Use only the measured player origin; no world-coordinate endpoint is supplied. "
            )
            + f"The requested egocentric corridor heading is "
            f"{float(corridor_heading if corridor_heading is not None else 0.0):.3f} radians "
            f"for {float(corridor_distance if corridor_distance is not None else 0.0):.3f} world units. "
            "The native character-forward axis is zero (0 radians); positive angles turn to "
            "+pi/2 toward character-left, negative angles turn toward character-right, and "
            "pi is directly behind. These headings are character-relative, never camera-relative. "
            "Use only a measured bearing, visible corridor extent, and measured world units and "
            "scale "
            "that the crop actually proves. The camera must be sufficiently aligned to prove "
            "that character-relative direction; if direction, visible extent, scale, or camera "
            "alignment is not observable, refuse honestly with an empty routeId and false flags. "
            "Never infer a route from process state; use an empty routeId "
            "and false flags when the path is not visibly proved."
        )
        self.neutral_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.neutral_dir, 0o700)
        with tempfile.TemporaryDirectory(prefix="crowley-model-route-", dir=self.neutral_dir) as temporary:
            directory = Path(temporary)
            image_path = directory / "screen.png"
            schema_path = directory / "route.json"
            image_path.write_bytes(image)
            os.chmod(image_path, 0o600)
            schema_path.write_text(self._route_schema(), encoding="utf-8")
            os.chmod(schema_path, 0o600)
            stdout = self._run_cli(
                schema_path,
                image_path,
                prompt=route_prompt,
                timeout=timeout,
            )
            return self._parse_route_observation(stdout)

    def observe_viewport(
        self,
        image: bytes,
        *,
        target: str = "game-client",
        verified_crop: tuple[int, int, int, int] | None = None,
        timeout: float | None = None,
    ) -> ModelViewportObservation:
        """Measure one safe world-viewport point from a fresh client crop."""

        if (
            target != "game-client"
            or verified_crop is None
            or verified_crop[2] < 640
            or verified_crop[3] < 480
        ):
            raise ModelError("viewport observation lacks a verified client-area crop")
        self.neutral_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.neutral_dir, 0o700)
        with tempfile.TemporaryDirectory(prefix="crowley-model-viewport-", dir=self.neutral_dir) as temporary:
            directory = Path(temporary)
            image_path = directory / "screen.png"
            schema_path = directory / "viewport.json"
            image_path.write_bytes(image)
            os.chmod(image_path, 0o600)
            schema_path.write_text(self._viewport_schema(), encoding="utf-8")
            os.chmod(schema_path, 0o600)
            stdout = self._run_cli(
                schema_path,
                image_path,
                prompt=(
                    "You are the bounded Crowley world-viewport observer. Inspect only the "
                    "supplied verified game-client crop. Identify one physical pixel point "
                    "inside the visible 3D world that is clear of interface panels, chat, "
                    "action bars, NPCs, players, and other units. Return false flags and "
                    "point {x:0,y:0} when no such point is visibly proved. Never infer a "
                    "point from process state or an intended camera action."
                ),
                timeout=timeout,
            )
            return self._parse_viewport_observation(stdout)

    @staticmethod
    def _decision_schema() -> str:
        return (
            '{"type":"object","additionalProperties":false,"required":["action","target"],'
            '"properties":{"action":{"enum":["dismiss_disconnect","reconnect_game",'
            '"select_arthur","enter_world"]},"target":{"type":"string",'
            '"const":"game-client"}}}'
        )

    @staticmethod
    def _observation_schema() -> str:
        return (
            '{"type":"object","additionalProperties":false,"required":["state","facts",'
            '"anchor"],'
            '"properties":{"state":{"enum":["disconnect_dialog","game_client",'
            '"character_select","arthur_selected","world","auction_house_open",'
            '"mailbox_world","executor_mailbox"]},'
            '"facts":{"type":"object","additionalProperties":false,"required":'
            '["scene","character","realm","modal","code","control","idle"],"properties":'
            '{"scene":{"type":"string"},"character":{"type":"string"},'
            '"realm":{"type":"string"},"modal":{"type":"string"},'
            '"code":{"type":"string"},'
            '"control":{"type":"string"},"idle":{"type":"string"}}},'
            '"anchor":{"type":"object","additionalProperties":false,"required":'
            '["x","y","width","height"],"properties":{"x":{"type":"integer"},'
            '"y":{"type":"integer"},"width":{"type":"integer"},'
            '"height":{"type":"integer"}}}}}'
        )

    @staticmethod
    def _route_schema() -> str:
        return (
            '{"type":"object","additionalProperties":false,"required":['
            '"scene","routeId","targetVisible","groundClear"],"properties":{'
            '"scene":{"enum":["world"]},"routeId":{"type":"string","minLength":0},'
            '"targetVisible":{"type":"boolean"},"groundClear":{"type":"boolean"}}}'
        )

    @staticmethod
    def _viewport_schema() -> str:
        return (
            '{"type":"object","additionalProperties":false,"required":['
            '"scene","point","uiClear","unitsClear"],"properties":{'
            '"scene":{"enum":["world"]},"point":{"type":"object",'
            '"additionalProperties":false,"required":["x","y"],"properties":{'
            '"x":{"type":"integer"},"y":{"type":"integer"}}},'
            '"uiClear":{"type":"boolean"},"unitsClear":{"type":"boolean"}}}'
        )

    def _run_cli(
        self,
        schema_path: Path,
        image_path: Path,
        *,
        prompt: str,
        timeout: float | None = None,
    ) -> str:
        # Capture/model producers share one process and cancellation state.
        # Serialize calls so a second request cannot clear the first request's
        # cancellation flag or replace its process handle.
        if not self._model_call_lock.acquire(blocking=False):
            raise ModelError("another model call is already in flight")
        try:
            return self._run_cli_unlocked(
                schema_path,
                image_path,
                prompt=prompt,
                timeout=timeout,
            )
        finally:
            self._model_call_lock.release()

    @staticmethod
    def _terminate_process_bounded(process: subprocess.Popen[str], deadline: float) -> bool:
        """Terminate and reap one model process without crossing its deadline."""

        if process.poll() is not None:
            return True
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.communicate(timeout=min(0.2, remaining))
            return process.poll() is not None
        except subprocess.TimeoutExpired:
            pass
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        remaining = max(0.0, deadline - time.monotonic())
        try:
            process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            return False
        return process.poll() is not None

    def _run_cli_unlocked(
        self,
        schema_path: Path,
        image_path: Path,
        *,
        prompt: str,
        timeout: float | None = None,
    ) -> str:
        with self._process_lock:
            retained = self._process
            if retained is not None and retained.poll() is None:
                raise ModelError("previous model process cleanup is still active")
            if retained is not None:
                self._process = None
            self._cancelled.clear()
            call_epoch = self._cancel_epoch
        budget = self.timeout if timeout is None else min(self.timeout, float(timeout))
        if not math.isfinite(budget) or budget <= 0:
            raise ModelError("model call has no remaining bounded deadline")
        deadline = time.monotonic() + budget
        args = self.command(
            schema_path,
            image_path,
            self.neutral_dir,
            executable=self.codex_path,
            prompt=prompt,
        )
        environment = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "XDG_RUNTIME_DIR", "DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY"}
        }
        try:
            version = subprocess.run(
                [self.codex_path, "--version"],
                check=True,
                capture_output=True,
                text=True,
                timeout=min(5.0, budget),
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ModelError(f"unable to verify Codex CLI: {exc}") from exc
        if version.stdout.strip() != self.cli_version:
            raise ModelError(f"unexpected Codex CLI version: {version.stdout.strip()!r}")
        if time.monotonic() >= deadline:
            raise ModelError("model decision timed out before process start")
        with self._process_lock:
            if call_epoch != self._cancel_epoch or self._cancelled.is_set():
                raise ModelError("model decision cancelled")
        process: subprocess.Popen[str] | None = None
        try:
            if not os.path.isfile(self.codex_path) or not os.access(self.codex_path, os.X_OK):
                raise ModelError(f"configured Codex executable is not executable: {self.codex_path}")
            process = subprocess.Popen(
                args,
                cwd=self.neutral_dir,
                env=environment,
                start_new_session=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
            )
            with self._process_lock:
                self._process = process
                cancelled_before_admission = call_epoch != self._cancel_epoch or self._cancelled.is_set()
            if cancelled_before_admission:
                self._terminate_process_bounded(process, deadline)
                raise ModelError("model decision cancelled")
            while True:
                if call_epoch != self._cancel_epoch or self._cancelled.is_set():
                    if not self._terminate_process_bounded(process, deadline):
                        raise ModelError("model decision cancellation cleanup exceeded its deadline")
                    raise ModelError("model decision cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if not self._terminate_process_bounded(process, deadline):
                        raise ModelError("model decision timeout cleanup exceeded its deadline")
                    raise ModelError("model decision timed out")
                try:
                    stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except OSError as exc:
            raise ModelError(f"unable to run model adapter: {exc}") from exc
        finally:
            with self._process_lock:
                if self._process is process and (process is None or process.poll() is not None):
                    self._process = None
        if self._cancelled.is_set():
            raise ModelError("model decision cancelled")
        if process.returncode != 0:
            raise ModelError(f"model CLI failed: {stderr[-500:]}")
        if stderr not in {"", KNOWN_STDERR_NOTICE}:
            # The structured JSON stream is the only accepted model boundary.
            # A credential prompt, billing/auth conflict, or an unclassified
            # startup diagnostic on stderr must stop the recovery attempt even
            # when the process exits zero with a plausible JSON answer.  The
            # pinned CLI's exact standalone stdin notice is informational.
            raise ModelError(f"model CLI emitted unexpected stderr: {stderr[-500:]}")
        return stdout

    @staticmethod
    def _parse(output: str) -> ModelDecision:
        answers, _diagnostics = LunaAdapter._parse_stream(output, answer_keys={"action", "target"})
        if len(answers) != 1:
            raise ModelError("model output must contain exactly one structured answer")
        decision = answers[0]
        action = decision.get("action")
        target = decision.get("target")
        if not isinstance(action, str) or not isinstance(target, str):
            raise ModelError("model decision enum fields must be strings")
        if set(decision) != {"action", "target"} or action not in DECISIONS or target != "game-client":
            raise ModelError("model output did not match the decision schema")
        return ModelDecision(action, target)

    @staticmethod
    def _parse_observation(output: str) -> ModelObservation:
        answers, _diagnostics = LunaAdapter._parse_stream(output, answer_keys={"state", "facts", "anchor"})
        if len(answers) != 1:
            raise ModelError("model output must contain exactly one structured observation")
        observation = answers[0]
        state = observation.get("state")
        if not isinstance(state, str):
            raise ModelError("model observation state must be a string")
        if set(observation) != {"state", "facts", "anchor"} or state not in OBSERVED_STATES:
            raise ModelError("model output did not contain a named visual state")
        facts = observation.get("facts")
        if (
            not isinstance(facts, dict)
            or set(facts)
            != {
                "scene",
                "character",
                "realm",
                "modal",
                "code",
                "control",
                "idle",
            }
            or any(not isinstance(value, str) for value in facts.values())
        ):
            raise ModelError("model observation facts do not match the strict schema")
        anchor = observation.get("anchor")
        if (
            not isinstance(anchor, dict)
            or set(anchor) != {"x", "y", "width", "height"}
            or any(isinstance(value, bool) or not isinstance(value, int) for value in anchor.values())
            or anchor["x"] < 0
            or anchor["y"] < 0
            or anchor["width"] < 0
            or anchor["height"] < 0
        ):
            raise ModelError("model observation anchor bounds do not match the strict schema")
        return ModelObservation(
            state,
            dict(facts),
            (anchor["x"], anchor["y"], anchor["width"], anchor["height"]),
        )

    @staticmethod
    def _parse_route_observation(output: str) -> ModelRouteObservation:
        answers, _diagnostics = LunaAdapter._parse_stream(
            output, answer_keys={"scene", "routeId", "targetVisible", "groundClear"}
        )
        if len(answers) != 1:
            raise ModelError("route model output must contain exactly one structured answer")
        value = answers[0]
        if set(value) != {"scene", "routeId", "targetVisible", "groundClear"}:
            raise ModelError("route model output did not match the strict schema")
        if (
            value["scene"] != "world"
            or not isinstance(value["routeId"], str)
            or (not value["routeId"] and (value["targetVisible"] or value["groundClear"]))
            or not isinstance(value["targetVisible"], bool)
            or not isinstance(value["groundClear"], bool)
        ):
            raise ModelError("route model facts are malformed")
        return ModelRouteObservation(
            value["scene"], value["routeId"], value["targetVisible"], value["groundClear"]
        )

    @staticmethod
    def _parse_viewport_observation(output: str) -> ModelViewportObservation:
        answers, _diagnostics = LunaAdapter._parse_stream(
            output, answer_keys={"scene", "point", "uiClear", "unitsClear"}
        )
        if len(answers) != 1:
            raise ModelError("viewport model output must contain exactly one structured answer")
        value = answers[0]
        if set(value) != {"scene", "point", "uiClear", "unitsClear"}:
            raise ModelError("viewport model output did not match the strict schema")
        point = value["point"]
        if (
            value["scene"] != "world"
            or not isinstance(point, dict)
            or set(point) != {"x", "y"}
            or any(
                isinstance(component, bool) or not isinstance(component, int) for component in point.values()
            )
            or point["x"] < 0
            or point["y"] < 0
            or not isinstance(value["uiClear"], bool)
            or not isinstance(value["unitsClear"], bool)
        ):
            raise ModelError("viewport model facts are malformed")
        return ModelViewportObservation(
            value["scene"], (point["x"], point["y"]), value["uiClear"], value["unitsClear"]
        )

    @staticmethod
    def _parse_stream(output: str, *, answer_keys: set[str]) -> tuple[list[dict[str, Any]], int]:
        """Parse the small JSONL lifecycle emitted by the pinned Codex CLI.

        The CLI's structured answer is carried by exactly one completed
        ``agent_message`` item.  Treating arbitrary JSON objects, free text,
        or the last candidate-looking line as an answer made a diagnostic or
        an accidental second result silently replace the first one.  Keep the
        accepted lifecycle deliberately small and reject every other shape.
        """

        lifecycle = {
            "thread.started",
            "turn.started",
            "turn.completed",
            "turn.failed",
            "item.started",
            "item.completed",
            "error",
        }
        answers: list[dict[str, Any]] = []
        diagnostics = 0
        saw_thread = False
        saw_turn = False
        closed = False
        seen_item_ids: set[str] = set()
        lines = output.splitlines()
        if not lines or any(not line.strip() for line in lines):
            raise ModelError("model output contains an empty or missing lifecycle event")
        for index, line in enumerate(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ModelError("model output contains free text or malformed JSON") from exc
            if not isinstance(event, dict):
                # Lifecycle events may have an item payload only.  In
                # particular, output/result/action objects at the top level
                # are not part of the pinned JSONL contract.
                raise ModelError("model emitted an unknown result shape")
            event_type = event.get("type")
            if not isinstance(event_type, str):
                raise ModelError("model lifecycle event type must be a string")
            if event_type not in lifecycle:
                raise ModelError(f"model emitted an unknown lifecycle event: {event_type!r}")
            if LunaAdapter._contains_forbidden_event(event):
                raise ModelError("model emitted a tool or command event")
            if closed:
                raise ModelError("model emitted events after turn completion")
            if event_type == "thread.started":
                if index != 0 or saw_thread or set(event) != {"type", "thread_id"}:
                    raise ModelError("thread.started does not match the pinned lifecycle")
                if not isinstance(event["thread_id"], str) or not event["thread_id"]:
                    raise ModelError("thread.started carried an invalid thread ID")
                saw_thread = True
                continue
            if not saw_thread:
                raise ModelError("model lifecycle did not start with thread.started")
            if event_type in {"turn.failed", "error"}:
                raise ModelError("model emitted a failure event")
            if event_type == "turn.started":
                if saw_turn or set(event) != {"type"}:
                    raise ModelError("turn.started does not match the pinned lifecycle")
                saw_turn = True
                continue
            if event_type == "turn.completed":
                usage = event.get("usage")
                if not saw_turn or len(answers) != 1 or set(event) != {"type", "usage"}:
                    raise ModelError("turn.completed arrived out of order")
                if (
                    not isinstance(usage, dict)
                    or set(usage)
                    != {
                        "input_tokens",
                        "cached_input_tokens",
                        "cache_write_input_tokens",
                        "output_tokens",
                        "reasoning_output_tokens",
                    }
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, int)
                        or value < 0
                        or value > MAX_USAGE_TOKENS
                        for value in usage.values()
                    )
                ):
                    raise ModelError("turn.completed usage does not match the pinned lifecycle")
                closed = True
                continue
            if answers:
                raise ModelError("model emitted an item after the structured final answer")
            if event_type == "item.started":
                # At this tools-disabled boundary the exporter filters all
                # reasoning starts; every remaining item.started is tool or
                # todo activity and must be rejected.
                raise ModelError("item.started is not supported at the tools-disabled boundary")
            if event_type != "item.completed" or set(event) != {"type", "item"}:
                raise ModelError("lifecycle event does not match the pinned JSONL protocol")
            item = event["item"]
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
                raise ModelError("completed item identifier does not match the pinned lifecycle")
            item_id = item["id"]
            if item_id in seen_item_ids:
                raise ModelError("completed item ID was duplicated")
            item_type = item.get("type")
            if not isinstance(item_type, str):
                raise ModelError("completed item type must be a string")
            if item_type == "error":
                if not saw_thread or saw_turn:
                    raise ModelError("model diagnostic arrived out of order")
                if set(item) != {"id", "type", "message"} or item["message"] != KNOWN_DIAGNOSTIC:
                    raise ModelError("model emitted an unexpected diagnostic")
                if item_id in seen_item_ids or diagnostics >= 1:
                    raise ModelError("model emitted the benign diagnostic more than once")
                seen_item_ids.add(item_id)
                diagnostics += 1
                continue
            if item_type == "reasoning":
                if not saw_turn or set(item) != {"id", "type", "text"}:
                    raise ModelError("completed reasoning item does not match the pinned lifecycle")
                if not isinstance(item["text"], str) or not item["text"].strip():
                    raise ModelError("completed reasoning text does not match the pinned lifecycle")
                seen_item_ids.add(item_id)
                continue
            if item_type != "agent_message" or set(item) != {"id", "type", "text"} or not saw_turn:
                raise ModelError("completed item is not a structured agent message")
            if item_id in seen_item_ids or answers:
                raise ModelError("model emitted multiple structured answers")
            text = item["text"]
            if not isinstance(text, str) or not text:
                raise ModelError("agent message was not structured JSON text")
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ModelError("agent message was not structured JSON") from exc
            if not isinstance(parsed, dict) or set(parsed) != answer_keys:
                raise ModelError("agent message does not match the exact answer schema")
            if LunaAdapter._contains_forbidden_event(parsed):
                raise ModelError("model emitted a tool or command event")
            seen_item_ids.add(item_id)
            answers.append(parsed)
        if not saw_thread or not saw_turn or not closed or len(answers) != 1:
            raise ModelError("model output did not contain one complete pinned turn")
        return answers, diagnostics

    @staticmethod
    def _walk(value: Any):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from LunaAdapter._walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from LunaAdapter._walk(child)

    @staticmethod
    def _contains_forbidden_event(value: Any) -> bool:
        forbidden = {
            "tool_call",
            "function_call",
            "command_execution",
            "tool_use",
            "computer_call",
            "shell_command",
            "mcp_tool_call",
            "web_search",
            "file_change",
        }
        for item in LunaAdapter._walk(value):
            if "type" not in item:
                continue
            item_type = item["type"]
            if not isinstance(item_type, str):
                raise ModelError("recursive event type must be a string")
            if item_type in forbidden:
                return True
        return False


__all__ = [
    "CLI_VERSION",
    "DECISIONS",
    "LunaAdapter",
    "ModelDecision",
    "ModelError",
    "ModelObservation",
    "ModelRouteObservation",
    "ModelViewportObservation",
    "OBSERVED_CONTROLS",
    "OBSERVED_STATES",
]
