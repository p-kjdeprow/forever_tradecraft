"""Independent owner-death release watchdog.

This module deliberately has no dependency on the guardian core or driver.  The
spawned watchdog receives only its release capability and framed owner state.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from ..input_tool import NativeReleaseSpec


def _proc_start(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()[21]
    except (OSError, IndexError):
        return None


def _watchdog_entry(
    owner_pid: int,
    owner_start: str | None,
    read_fd: int,
    interval: float,
    release_command: tuple[str, ...],
    release_env: dict[str, str],
    write_fd: int | None = None,
    release_fd: int | None = None,
    ready_fd: int | None = None,
    release_spec: NativeReleaseSpec | None = None,
    ack_fd: int | None = None,
    process_start: Callable[[int], str | None] | None = None,
) -> None:
    """Independent owner-death watchdog; it must not import the driver."""

    # ``spawn`` transports descriptors as reduction wrappers.  Accept both
    # those wrappers and plain integers so the entrypoint remains usable by a
    # small isolated process smoke test.
    if hasattr(read_fd, "detach"):
        read_fd = read_fd.detach()
    if hasattr(write_fd, "detach"):
        write_fd = write_fd.detach()
    if hasattr(release_fd, "detach"):
        release_fd = release_fd.detach()
    if hasattr(ready_fd, "detach"):
        ready_fd = ready_fd.detach()
    if hasattr(ack_fd, "detach"):
        ack_fd = ack_fd.detach()
    if release_spec is not None and hasattr(release_spec.fd, "detach"):
        release_spec = NativeReleaseSpec(release_spec.fd.detach(), release_spec.digest, release_spec.version)

    if release_spec is not None:
        try:
            release_spec.verify_image()
        except Exception:
            for descriptor in (read_fd, release_spec.fd, ack_fd, ready_fd):
                if isinstance(descriptor, int) and descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            return
    elif release_fd is not None:
        # Native release capabilities must be typed. A bare descriptor is
        # accepted nowhere in production and is closed before startup.
        try:
            os.close(release_fd)
        except OSError:
            pass
        release_fd = None

    held = False
    held_keys: tuple[str, ...] = ()
    held_buttons: tuple[int, ...] = ()
    active_groups: tuple[int, ...] = ()

    # The owner keeps only the write end; close the child's copy so owner
    # shutdown reaches EOF on the command channel.
    if write_fd is not None:
        try:
            os.close(write_fd)
        except OSError:
            pass
    # Retain only its command channel and standard descriptors; the watchdog
    # never needs unrelated application descriptors.
    if write_fd is not None:
        try:
            inherited = os.listdir("/proc/self/fd")
        except OSError:
            inherited = ()
        for value in inherited:
            try:
                fd = int(value)
            except ValueError:
                continue
            retained = {read_fd, write_fd, ready_fd, ack_fd}
            if release_spec is not None:
                retained.add(release_spec.fd)
            if fd <= 2 or fd in retained:
                continue
            try:
                os.close(fd)
            except OSError:
                pass

    def terminate_groups() -> bool:
        nonlocal active_groups
        succeeded = True
        for pgid in active_groups:
            if pgid <= 0 or pgid == os.getpgrp():
                succeeded = False
                continue
            try:
                os.killpg(pgid, signal.SIGTERM)
                # The group is owned by the session wrapper.  Escalate in the
                # watchdog itself so an owner crash cannot leave a command
                # running after it ignores the polite termination.
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except OSError:
                succeeded = False
        if succeeded:
            active_groups = ()
        return succeeded

    def release() -> bool:
        nonlocal held, held_keys, held_buttons
        if not held:
            return True
        commands: list[tuple[str, ...]] = []
        # The native driver gives the watchdog the exact keys/buttons that were
        # held when the owner died. ``xdotool keyup --all`` is unsupported on
        # several installed versions and would also release unrelated input.
        native_release = release_spec is not None
        if native_release:
            executable = f"/proc/self/fd/{release_spec.fd}"
            commands.extend((executable, "keyup", key) for key in held_keys)
            commands.extend((executable, "mouseup", str(button)) for button in held_buttons)
        elif release_command:
            # Fake/test guardians may provide a complete helper command. The
            # native production mode above is the only item-expanding path.
            commands.append(release_command)
        if not commands:
            # A malformed native held-state message must never be treated as
            # proof that physical input is released.  Retain the watchdog
            # until the owner can publish the exact tracked items or an
            # operator closes the lease safely.
            return False
        succeeded = True
        for command in commands:
            try:
                pass_fds = (release_spec.fd,) if native_release else ()
                result = subprocess.run(
                    list(command),
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=release_env,
                    timeout=3,
                    pass_fds=pass_fds,
                )
                if result.returncode != 0:
                    succeeded = False
            except (OSError, subprocess.SubprocessError):
                succeeded = False
        if succeeded:
            held = False
            held_keys = ()
            held_buttons = ()
        return succeeded

    if ready_fd is not None:
        try:
            os.write(ready_fd, b"READY\n")
        except OSError:
            return
        finally:
            try:
                os.close(ready_fd)
            except OSError:
                pass

    def acknowledge() -> bool:
        if ack_fd is None:
            return True
        try:
            return os.write(ack_fd, b"ACK\n") == 4
        except OSError:
            return False

    def apply_message(message: bytes) -> bool:
        nonlocal held, held_keys, held_buttons, active_groups
        if not message:
            return True
        if message.startswith(b"S"):
            while active_groups and not terminate_groups():
                time.sleep(min(interval, 0.25))
            while held and not release():
                time.sleep(min(interval, 0.25))
            return True
        if message.startswith(b"T"):
            try:
                state = json.loads(message[1:].decode("utf-8"))
                groups = state.get("groups", ())
                keys = state.get("keys", ())
                buttons = state.get("buttons", ())
                if (
                    not isinstance(groups, list)
                    or not isinstance(keys, list)
                    or not isinstance(buttons, list)
                ):
                    raise ValueError("watchdog state lists are malformed")
                active_groups = tuple(sorted({int(group) for group in groups if int(group) > 0}))
                held_keys = tuple(str(key) for key in keys)
                held_buttons = tuple(int(button) for button in buttons)
                held = bool(held_keys or held_buttons)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                return False
            # A lost ACK channel must not discard this complete state. The
            # owner will fail closed; the watchdog must retain the latest
            # state for an eventual EOF/owner-death release.
            acknowledge()
            return True
        if message.startswith(b"P"):
            try:
                state = json.loads(message[1:].decode("utf-8"))
                active_groups = tuple(sorted({int(group) for group in state if int(group) > 0}))
            except (ValueError, TypeError, json.JSONDecodeError):
                return False
            acknowledge()
            return True
        if message.startswith(b"R"):
            held = False
            held_keys = ()
            held_buttons = ()
            acknowledge()
            return True
        if not message.startswith(b"H"):
            return False
        if message == b"H":
            held = True
            held_keys = ()
            held_buttons = ()
            acknowledge()
            return True
        try:
            state = json.loads(message[1:].decode("utf-8"))
            if not isinstance(state, dict):
                raise ValueError("watchdog hold state is malformed")
            held_keys = tuple(str(key) for key in state.get("keys", ()))
            held_buttons = tuple(int(button) for button in state.get("buttons", ()))
        except (ValueError, TypeError, json.JSONDecodeError):
            return False
        held = True
        acknowledge()
        return True

    def cleanup() -> None:
        while active_groups and not terminate_groups():
            time.sleep(min(interval, 0.25))
        while not release():
            time.sleep(min(interval, 0.25))

    resolve_process_start = _proc_start if process_start is None else process_start
    try:
        pending = b""
        os.set_blocking(read_fd, False)
        while True:
            ready, _, _ = select.select([read_fd], [], [], interval)
            if ready:
                try:
                    message = os.read(read_fd, 4096)
                except BlockingIOError:
                    message = None
                if message is None:
                    continue
                if message == b"":
                    # EOF is reached only after all bytes in the pipe have
                    # been consumed. Complete newline-framed records are
                    # applied; an incomplete final frame is discarded safely.
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        if not apply_message(line):
                            cleanup()
                            return
                    cleanup()
                    return
                pending += message
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if not apply_message(line):
                        cleanup()
                        return
            current = resolve_process_start(owner_pid)
            if current is None or (owner_start is not None and current != owner_start):
                # Drain all complete records queued before owner death before
                # touching the held state. This closes the final-H race.
                while True:
                    try:
                        extra = os.read(read_fd, 4096)
                    except BlockingIOError:
                        break
                    if not extra:
                        break
                    pending += extra
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if not apply_message(line):
                        break
                cleanup()
                return
    finally:
        try:
            os.close(read_fd)
        except OSError:
            pass
        for descriptor in (ack_fd, release_spec.fd if release_spec is not None else None):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
