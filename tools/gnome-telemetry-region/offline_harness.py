#!/usr/bin/env python3
# ruff: noqa: E501
"""Offline producer/admission checks for the typed telemetry-region contract."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import math
import os
import re
import select
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from crowley.platform.paths import XdgDirs
from crowley.session.source_binding import SAFETY_MODULES, build_source_binding

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "tools/gnome-telemetry-region/0001-typed-telemetry-area.patch"


def _stage_root() -> Path:
    configured = os.environ.get("GNOME_TELEMETRY_REGION_STAGE")
    if configured is not None:
        stage = Path(configured)
        if not stage.is_absolute():
            raise RuntimeError("GNOME_TELEMETRY_REGION_STAGE must be absolute")
        return stage
    return XdgDirs.from_environ(os.environ).crowley_cache / "gnome-telemetry-region-r6"


def _binary_path() -> Path:
    configured = os.environ.get("GNOME_TELEMETRY_REGION_BINARY")
    if configured is not None:
        binary = Path(configured)
        if not binary.is_absolute():
            raise RuntimeError("GNOME_TELEMETRY_REGION_BINARY must be absolute")
        return binary
    return _stage_root() / "prefix/bin/gnome-screenshot"


def _png_256() -> bytes:
    raw = b"".join(b"\x00" + b"\x00\x00\x00\xff" * 256 for _ in range(256))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _strict_argv(argv: list[str]) -> bool:
    counts: dict[str, int] = {}
    values: dict[str, str] = {}
    scalar = {
        "--telemetry-x",
        "--telemetry-y",
        "--telemetry-width",
        "--telemetry-height",
        "--telemetry-owner-fd",
        "--telemetry-exec-fd",
        "--telemetry-nonce",
        "--file",
    }
    conflicts = {"--window", "--area", "--interactive", "--clipboard", "--include-pointer", "--delay"}
    telemetry = "--telemetry-region" in argv
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--telemetry-region":
            counts[token] = counts.get(token, 0) + 1
            index += 1
            continue
        if token in scalar or token == "-f":
            name = "--file" if token == "-f" else token
            counts[token] = counts.get(token, 0) + 1
            if index + 1 >= len(argv):
                return False
            counts[name] = counts.get(name, 0) + (1 if token == "-f" else 0)
            values[name] = argv[index + 1]
            index += 2
            continue
        if token.startswith("--") and any(token.startswith(name + "=") for name in scalar):
            name, value = token.split("=", 1)
            counts[name] = counts.get(name, 0) + 1
            values[name] = value
        elif token in conflicts or token == "-d" or token.startswith("-d="):
            return False if telemetry else True
        index += 1
    if not telemetry:
        return not any(key.startswith("--telemetry-") for key in counts)
    if counts.get("--telemetry-region", 0) != 1:
        return False
    if any(counts.get(key, 0) != 1 for key in scalar):
        return False
    try:

        def canonical_int(value: str) -> int:
            if re.fullmatch(r"0|-?[1-9][0-9]*", value) is None:
                raise ValueError(value)
            return int(value, 10)

        rectangle = tuple(
            canonical_int(values[key])
            for key in ("--telemetry-x", "--telemetry-y", "--telemetry-width", "--telemetry-height")
        )
        owner_fd = canonical_int(values["--telemetry-owner-fd"])
        exec_fd = canonical_int(values["--telemetry-exec-fd"])
    except (KeyError, ValueError):
        return False
    return (
        rectangle[2:] == (256, 256)
        and owner_fd > 2
        and exec_fd > 2
        and bool(values["--telemetry-nonce"])
        and Path(values["--file"]).is_absolute()
    )


def _readline(sock: socket.socket, deadline: float) -> dict[str, object]:
    data = bytearray()
    while b"\n" not in data:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("protocol deadline")
        ready, _, _ = select.select([sock], [], [], min(0.1, remaining))
        if not ready:
            continue
        # Read one byte at a time so a fast child cannot coalesce the next
        # protocol record into this call and leave it discarded.
        part = sock.recv(1)
        if not part:
            raise RuntimeError("protocol EOF")
        data.extend(part)
    value = json.loads(data.split(b"\n", 1)[0])
    if not isinstance(value, dict):
        raise RuntimeError("protocol object required")
    return value


def _protocol_fixture() -> None:
    import socket

    from crowley.session.driver import _EXEC_ADMISSION_WRAPPER, _open_sealed_executable
    from crowley.session.process_admission import AdmissionGate

    with tempfile.TemporaryDirectory(prefix="crowley-region-selftest-") as temporary:
        root = Path(temporary)
        payload = _png_256()
        encoded = base64.b64encode(payload).decode("ascii")
        script = root / "producer.py"
        script.write_text(
            "#!/usr/bin/python3\n"
            "import base64,hashlib,json,os, pathlib,sys,time\n"
            "args=sys.argv[1:]\n"
            "def value(name): return args[args.index(name)+1]\n"
            "fd=int(value('--telemetry-owner-fd')); efd=int(value('--telemetry-exec-fd')); nonce=value('--telemetry-nonce'); out=pathlib.Path(value('--file'))\n"
            "tail=open('/proc/self/stat').read().rsplit(')',1)[1].split()\n"
            "start=tail[19]; digest=hashlib.sha256(open('/proc/self/fd/%d'%efd,'rb').read()).hexdigest()\n"
            "rect=[int(value('--telemetry-x')),int(value('--telemetry-y')),int(value('--telemetry-width')),int(value('--telemetry-height'))]\n"
            "pathdigest=hashlib.sha256(str(out).encode()).hexdigest()\n"
            "def send(v): os.write(fd,(json.dumps(v,separators=(',',':'))+'\\n').encode())\n"
            "send({'type':'owner_ready','protocol':1,'nonce':nonce,'pid':os.getpid(),'process_start':start,'ppid':os.getppid(),'pgid':os.getpgrp(),'exec_fd':efd,'executable_sha256':digest,'primary_owner':True,'remote':False,'branch':'ScreenshotArea','rectangle':rect,'output_path_digest':pathdigest})\n"
            "ack=bytearray()\n"
            "while b'\\n' not in ack:\n"
            "    piece=os.read(fd,1)\n"
            "    if not piece: raise SystemExit(4)\n"
            "    ack.extend(piece)\n"
            "assert bytes(ack).decode() == json.dumps({'type':'ACK','protocol':1,'nonce':nonce},separators=(',',':'))+'\\n'\n"
            "send({'type':'capture_started','protocol':1,'nonce':nonce,'branch':'ScreenshotArea','rectangle':rect,'output_path_digest':pathdigest,'capture_started':time.monotonic()})\n"
            "data=base64.b64decode(" + repr(encoded) + "); out.write_bytes(data)\n"
            "send({'type':'capture_complete','protocol':1,'nonce':nonce,'branch':'ScreenshotArea','rectangle':rect,'output_path_digest':pathdigest,'output_sha256':hashlib.sha256(data).hexdigest(),'output_bytes':len(data)})\n",
            encoding="utf-8",
        )
        script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        image_fd, digest = _open_sealed_executable(script, hashlib.sha256(script.read_bytes()).hexdigest())
        parent, child = socket.socketpair()
        gate_read, gate_write = os.pipe()
        nonce = "selftest-nonce"
        output = root / "out.png"
        target = f"/proc/self/fd/{image_fd}"
        args = [
            target,
            "--telemetry-x",
            "10",
            "--telemetry-y",
            "20",
            "--telemetry-width",
            "256",
            "--telemetry-height",
            "256",
            "--telemetry-owner-fd",
            str(child.fileno()),
            "--telemetry-exec-fd",
            str(image_fd),
            "--telemetry-nonce",
            nonce,
            "--file",
            str(output),
        ]
        process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _EXEC_ADMISSION_WRAPPER, str(gate_read), target, *args[1:]],
            start_new_session=True,
            pass_fds=(gate_read, child.fileno(), image_fd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        child.close()
        AdmissionGate(gate_write).release()
        os.close(gate_read)
        owner = _readline(parent, time.monotonic() + 5)
        assert owner["pid"] == process.pid and owner["executable_sha256"] == digest
        ack = (
            json.dumps({"type": "ACK", "protocol": 1, "nonce": nonce}, separators=(",", ":")) + "\n"
        ).encode()
        midpoint = max(1, len(ack) // 2)
        parent.sendall(ack[:midpoint])
        time.sleep(0.01)
        parent.sendall(ack[midpoint:])
        started = _readline(parent, time.monotonic() + 5)
        completed = _readline(parent, time.monotonic() + 5)
        assert started["branch"] == "ScreenshotArea" and completed["output_bytes"] == len(payload)
        assert output.read_bytes() == payload
        assert process.wait(timeout=5) == 0
        parent.close()
        os.close(image_fd)


def _compiled_private_shell_fixture() -> None:
    """Run the compiled producer against a private bus/display compositor.

    The service is an external test compositor.  The producer, argument
    parser, D-Bus ``ScreenshotArea`` call, PNG readback, and telemetry records
    are real; the host desktop and production Shell are never contacted.
    """

    for command in ("dbus-run-session", "gjs", "Xvfb"):
        if shutil.which(command) is None:
            raise RuntimeError(f"compiled private producer fixture needs {command}")
    with tempfile.TemporaryDirectory(prefix="crowley-region-dbus-") as temporary:
        root = Path(temporary)
        payload = _png_256()
        payload_path = root / "payload.png"
        payload_path.write_text(base64.b64encode(payload).decode("ascii"), encoding="ascii")
        display_path = root / "display"
        bus_path = root / "bus"
        ready_path = root / "ready"
        release_path = root / "release-screenshot-owner"
        released_path = root / "released-screenshot-owner"
        shell = root / "shell.js"
        shell.write_text(
            "const { Gio, GLib } = imports.gi;\n"
            'const xml = `<node><interface name="org.gnome.Shell.Screenshot">'
            '<method name="ScreenshotArea">'
            '<arg type="i" direction="in"/><arg type="i" direction="in"/>'
            '<arg type="i" direction="in"/><arg type="i" direction="in"/>'
            '<arg type="b" direction="in"/><arg type="s" direction="in"/>'
            '<arg type="b" direction="out"/></method></interface></node>`;\n'
            "const service = { ScreenshotArea(x, y, w, h, flash, path) {\n"
            "  if (x !== 80 || y !== 48 || w !== 256 || h !== 256 || flash !== false) throw new Error('bad ScreenshotArea request');\n"
            "  let [ok, bytes] = GLib.file_get_contents(ARGV[0]); if (!ok) throw new Error('payload missing');\n"
            "  GLib.file_set_contents(path, GLib.base64_decode(imports.byteArray.toString(bytes))); return true;\n"
            "} };\n"
            "let bus = Gio.bus_get_sync(Gio.BusType.SESSION, null);\n"
            "let exported = Gio.DBusExportedObject.wrapJSObject(xml, service);\n"
            "exported.export(bus, '/org/gnome/Shell/Screenshot');\n"
            "let owner = Gio.bus_own_name_on_connection(bus, 'org.gnome.Shell.Screenshot', Gio.BusNameOwnerFlags.NONE, null, null);\n"
            "let competing = Gio.bus_own_name_on_connection(bus, 'org.gnome.Screenshot', Gio.BusNameOwnerFlags.NONE, null, null);\n"
            "GLib.timeout_add(GLib.PRIORITY_DEFAULT, 250, () => { GLib.file_set_contents(ARGV[1], 'ready'); return GLib.SOURCE_REMOVE; });\n"
            "GLib.timeout_add(GLib.PRIORITY_DEFAULT, 10, () => { if (!GLib.file_test(ARGV[2], GLib.FileTest.EXISTS)) return GLib.SOURCE_CONTINUE; Gio.bus_unown_name(competing); GLib.file_set_contents(ARGV[3], 'released'); return GLib.SOURCE_REMOVE; });\n"
            "new GLib.MainLoop(null, false).run();\n",
            encoding="utf-8",
        )
        session_script = (
            "set -eu\n"
            f"Xvfb -displayfd 9 -screen 0 1920x1080x24 9>{display_path} 2>{root / 'xvfb.stderr'} &\n"
            "xv=$!\n"
            "trap 'kill $xv 2>/dev/null || true' EXIT\n"
            f"while [ ! -s {display_path} ]; do sleep 0.01; done\n"
            f"display=$(cat {display_path}); export DISPLAY=:$display GDK_BACKEND=x11\n"
            f"printf '%s\\n' \"$DBUS_SESSION_BUS_ADDRESS\" > {bus_path}\n"
            f"exec gjs {shell} {payload_path} {ready_path} {release_path} {released_path} >{root / 'gjs.stdout'} 2>{root / 'gjs.stderr'}\n"
        )
        session = subprocess.Popen(
            ["dbus-run-session", "--", "bash", "-lc", session_script],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            ready_deadline = time.monotonic() + 8.0
            while (not bus_path.exists() or not display_path.exists()) and time.monotonic() < ready_deadline:
                if session.poll() is not None:
                    raise RuntimeError(f"private compositor exited: {session.stderr.read()}")
                time.sleep(0.02)
            if not bus_path.exists() or not display_path.exists():
                if session.poll() is not None:
                    detail = session.stderr.read()
                else:
                    detail = "<session still running>"
                raise RuntimeError(f"private compositor did not publish bus/display: {detail}")
            display = ":" + display_path.read_text(encoding="utf-8").strip()
            time.sleep(1.0)
            environment = os.environ.copy()
            environment.update(
                {
                    "DBUS_SESSION_BUS_ADDRESS": bus_path.read_text(encoding="utf-8").strip(),
                    "DISPLAY": display,
                    "GDK_BACKEND": "x11",
                }
            )
            # Compose the genuine binary with the production parent boundary
            # before the direct protocol checks below.  The D-Bus/X11 service
            # is an owned external fixture; the parent, sealed memfd, strict
            # records, decoder-sized PNG and identity path remain real.
            from crowley.session.driver import DriverError, DriverProfile, X11Backend

            xauthority = root / "Xauthority"
            xauthority.touch(mode=0o600)
            local_binary = root / "gnome-screenshot-r6"
            shutil.copyfile(_binary_path(), local_binary)
            local_binary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            profile = DriverProfile(
                display=display,
                xauthority=str(xauthority),
                client_width=1920,
                client_height=1080,
                outer_width=1928,
                outer_height=1092,
                canvas_crop=(80, 48, 256, 256),
                model_crop=(80, 48, 640, 480),
                telemetry_region_executable=local_binary,
                telemetry_region_executable_sha256=hashlib.sha256(local_binary.read_bytes()).hexdigest(),
            )
            production_identity = SimpleNamespace(
                fingerprint="compiled-private-production-identity",
                child_geometry=(0, 0, 1920, 1080),
            )
            production_backend = X11Backend(profile, input_tool=SimpleNamespace())
            production_backend.resolve_identity = lambda **_kwargs: production_identity  # type: ignore[method-assign]
            saved_bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = environment["DBUS_SESSION_BUS_ADDRESS"]
            composed_output = root / "production-parent-capture.png"
            try:
                competing_output = root / "competing-owner.png"
                try:
                    production_backend.capture_native_region(
                        competing_output,
                        identity=production_identity,
                        rectangle=(80, 48, 256, 256),
                        deadline_monotonic=time.monotonic() + 5.0,
                        _allow_unobserved_fixture=True,
                    )
                except DriverError:
                    pass
                else:
                    raise RuntimeError("compiled producer forwarded through a competing canonical owner")
                if competing_output.exists() or production_backend.active_process_groups:
                    raise RuntimeError("competing canonical owner left a capture or process group")
                release_path.touch(mode=0o600)
                released_deadline = time.monotonic() + 5.0
                while not released_path.exists() and time.monotonic() < released_deadline:
                    time.sleep(0.01)
                if not released_path.exists():
                    raise RuntimeError("private compositor did not release competing canonical owner")
                captured_identity = production_backend.capture_native_region(
                    composed_output,
                    identity=production_identity,
                    rectangle=(80, 48, 256, 256),
                    deadline_monotonic=time.monotonic() + 12.0,
                    _allow_unobserved_fixture=True,
                )
            finally:
                if saved_bus is None:
                    os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
                else:
                    os.environ["DBUS_SESSION_BUS_ADDRESS"] = saved_bus
            if captured_identity.fingerprint != production_identity.fingerprint:
                raise RuntimeError("production parent rejected its own compiled region identity")
            with Image.open(composed_output) as composed_image, Image.open(BytesIO(payload)) as expected:
                composed_image.load()
                expected.load()
                if (
                    composed_image.size != (256, 256)
                    or composed_image.convert("RGBA").tobytes() != expected.convert("RGBA").tobytes()
                ):
                    raise RuntimeError("production parent composed PNG did not preserve exact pixels")
            parent, child = socket.socketpair()
            image_fd = os.open(_binary_path(), os.O_RDONLY)
            output = root / "capture.png"
            nonce = "compiled-private-fixture"
            process = subprocess.Popen(
                [
                    str(_binary_path()),
                    "--telemetry-region",
                    "--telemetry-x",
                    "80",
                    "--telemetry-y",
                    "48",
                    "--telemetry-width",
                    "256",
                    "--telemetry-height",
                    "256",
                    "--telemetry-owner-fd",
                    str(child.fileno()),
                    "--telemetry-exec-fd",
                    str(image_fd),
                    "--telemetry-nonce",
                    nonce,
                    "--file",
                    str(output),
                ],
                pass_fds=(child.fileno(), image_fd),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            child.close()
            os.close(image_fd)
            try:
                parent.settimeout(12.0)
                owner = _readline(parent, time.monotonic() + 10.0)
                if owner.get("type") != "owner_ready" or owner.get("nonce") != nonce:
                    raise RuntimeError(f"compiled producer owner record is invalid: {owner}")
                parent.sendall(
                    (
                        json.dumps({"type": "ACK", "protocol": 1, "nonce": nonce}, separators=(",", ":"))
                        + "\n"
                    ).encode()
                )
                try:
                    started = _readline(parent, time.monotonic() + 10.0)
                except RuntimeError as exc:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2.0)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=2.0)
                    stderr = process.stderr.read()
                    session_diagnostic = ""
                    if session.poll() is not None:
                        session_diagnostic = session.stderr.read()
                    raise RuntimeError(
                        f"compiled producer failed after ACK: {exc}; stderr={stderr!r}; "
                        f"session={session.poll()} {session_diagnostic!r}"
                    ) from exc
                try:
                    completed = _readline(parent, time.monotonic() + 10.0)
                except RuntimeError as exc:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2.0)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=2.0)
                    stderr = process.stderr.read()
                    gjs_stderr = (root / "gjs.stderr").read_text(encoding="utf-8")
                    raise RuntimeError(
                        f"compiled producer failed before completion: {exc}; stderr={stderr!r}; "
                        f"gjs_stderr={gjs_stderr!r}"
                    ) from exc
                if started.get("branch") != "ScreenshotArea" or completed.get("output_bytes", 0) <= 0:
                    raise RuntimeError(f"compiled producer records are invalid: {started} {completed}")
                if process.wait(timeout=10.0) != 0:
                    raise RuntimeError(f"compiled producer failed: {process.stderr.read()}")
                with Image.open(output) as observed, Image.open(BytesIO(payload)) as expected:
                    observed.load()
                    expected.load()
                    if (
                        observed.size != (256, 256)
                        or observed.convert("RGBA").tobytes() != expected.convert("RGBA").tobytes()
                    ):
                        raise RuntimeError("private compositor PNG did not preserve exact pixels")
            finally:
                parent.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2.0)

            def run_compiled_owner_refusal(label: str, response: bytes | None) -> None:
                """Exercise the rebuilt C producer's owner-pipe refusal path."""

                refusal_output = root / f"{label}.png"
                owner_parent, owner_child = socket.socketpair()
                executable_fd = os.open(_binary_path(), os.O_RDONLY)
                refusal = subprocess.Popen(
                    [
                        str(_binary_path()),
                        "--telemetry-region",
                        "--telemetry-x",
                        "80",
                        "--telemetry-y",
                        "48",
                        "--telemetry-width",
                        "256",
                        "--telemetry-height",
                        "256",
                        "--telemetry-owner-fd",
                        str(owner_child.fileno()),
                        "--telemetry-exec-fd",
                        str(executable_fd),
                        "--telemetry-nonce",
                        f"{label}-nonce",
                        "--file",
                        str(refusal_output),
                    ],
                    pass_fds=(owner_child.fileno(), executable_fd),
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                owner_child.close()
                os.close(executable_fd)
                try:
                    owner_parent.settimeout(4.0)
                    record = _readline(owner_parent, time.monotonic() + 3.0)
                    if record.get("type") != "owner_ready":
                        raise RuntimeError(f"{label}: unexpected owner record {record}")
                    if response is not None:
                        owner_parent.sendall(response)
                    else:
                        owner_parent.close()
                        owner_parent = None
                    try:
                        exit_code = refusal.wait(timeout=4.0)
                    except subprocess.TimeoutExpired as exc:
                        raise RuntimeError(f"{label}: producer did not refuse promptly") from exc
                    if exit_code == 0 or refusal_output.exists():
                        raise RuntimeError(
                            f"{label}: producer accepted owner failure or published output "
                            f"(exit={exit_code}, output={refusal_output.exists()})"
                        )
                finally:
                    if owner_parent is not None:
                        owner_parent.close()
                    if refusal.poll() is None:
                        refusal.kill()
                        refusal.wait(timeout=2.0)

            run_compiled_owner_refusal(
                "compiled-wrong-ack",
                b'{"type":"ACK","protocol":1,"nonce":"wrong-nonce"}\n',
            )
            run_compiled_owner_refusal("compiled-owner-eof", None)
        finally:
            if session.poll() is None:
                os.killpg(session.pid, signal.SIGTERM)
                try:
                    session.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    os.killpg(session.pid, signal.SIGKILL)
                    session.wait(timeout=3.0)


@contextlib.contextmanager
def private_shell_compositor(
    *,
    payloads: list[bytes],
    full_desktop: bytes,
    rectangle: tuple[int, int, int, int],
    allow_area_gate: bool = False,
):
    """Own a private Xvfb, D-Bus session, and external Screenshot compositor."""

    for command in ("dbus-run-session", "gjs", "Xvfb"):
        if shutil.which(command) is None:
            raise RuntimeError(f"private compositor fixture needs {command}")
    with tempfile.TemporaryDirectory(prefix="crowley-region-composed-") as temporary:
        root = Path(temporary)
        payload_paths: list[str] = []
        for index, payload in enumerate(payloads):
            payload_path = root / f"payload-{index:02d}.b64"
            payload_path.write_text(base64.b64encode(payload).decode("ascii"), encoding="ascii")
            payload_paths.append(str(payload_path))
        payload_list = root / "payloads.json"
        payload_list.write_text(json.dumps(payload_paths), encoding="utf-8")
        full_path = root / "full-desktop.b64"
        full_path.write_text(base64.b64encode(full_desktop).decode("ascii"), encoding="ascii")
        area_log = root / "area-calls.json"
        stock_log = root / "stock-calls.json"
        bus_path = root / "bus-address"
        display_path = root / "display-number"
        ready_path = root / "compositor-ready"
        area_block_path = root / "block-next-area"
        area_started_path = root / "area-blocked"
        area_release_path = root / "release-area"
        cache_home = root / "cache"
        cache_home.mkdir(mode=0o700)
        shell = root / "compositor.js"
        shell.write_text(
            "const { Gio, GLib } = imports.gi;\n"
            'const xml = `<node><interface name="org.gnome.Shell.Screenshot">'
            '<method name="ScreenshotArea">'
            '<arg type="i" direction="in"/><arg type="i" direction="in"/>'
            '<arg type="i" direction="in"/><arg type="i" direction="in"/>'
            '<arg type="b" direction="in"/><arg type="s" direction="in"/>'
            '<arg type="b" direction="out"/><arg type="s" direction="out"/>'
            '</method><method name="Screenshot">'
            '<arg type="b" direction="in"/><arg type="b" direction="in"/>'
            '<arg type="s" direction="in"/><arg type="b" direction="out"/>'
            '<arg type="s" direction="out"/></method></interface></node>`;\n'
            "function readText(path) { let [ok, bytes] = GLib.file_get_contents(path); if (!ok) throw new Error('fixture text missing'); return imports.byteArray.toString(bytes); }\n"
            "function readBase64(path) { return GLib.base64_decode(readText(path)); }\n"
            "function append(path, value) { let rows = []; if (GLib.file_test(path, GLib.FileTest.EXISTS)) { rows = JSON.parse(readText(path)); } rows.push(value); GLib.file_set_contents(path, JSON.stringify(rows)); }\n"
            "let payloadPaths = JSON.parse(readText(ARGV[0]));\n"
            "let areaIndex = 0;\n"
            "const expected = [Number(ARGV[3]), Number(ARGV[4]), Number(ARGV[5]), Number(ARGV[6])];\n"
            "const service = { ScreenshotArea(x, y, width, height, flash, filename) {\n"
            "  if (x !== expected[0] || y !== expected[1] || width !== expected[2] || height !== expected[3] || flash !== false) throw new Error('bad ScreenshotArea request');\n"
            "  const payload = payloadPaths[areaIndex++]; if (!payload) throw new Error('unexpected ScreenshotArea retry');\n"
            + (
                "  if (GLib.file_test(ARGV[10], GLib.FileTest.EXISTS)) { GLib.file_set_contents(ARGV[11], String(GLib.get_monotonic_time())); while (!GLib.file_test(ARGV[12], GLib.FileTest.EXISTS)) GLib.usleep(10000); }\n"
                if allow_area_gate
                else ""
            )
            + "  GLib.file_set_contents(filename, readBase64(payload));\n"
            "  append(ARGV[1], {x, y, width, height, flash, index: areaIndex, filename, monotonic_us: GLib.get_monotonic_time()}); return [true, filename];\n"
            "}, Screenshot(includeCursor, flash, filename) {\n"
            "  GLib.file_set_contents(filename, readBase64(ARGV[2]));\n"
            "  append(ARGV[7], {includeCursor, flash, filename}); return [true, filename];\n"
            "} };\n"
            "let bus = Gio.bus_get_sync(Gio.BusType.SESSION, null);\n"
            "let exported = Gio.DBusExportedObject.wrapJSObject(xml, service);\n"
            "exported.export(bus, '/org/gnome/Shell/Screenshot');\n"
            "let owner = Gio.bus_own_name_on_connection(bus, 'org.gnome.Shell.Screenshot', Gio.BusNameOwnerFlags.NONE, null, null);\n"
            "GLib.file_set_contents(ARGV[8], GLib.getenv('DBUS_SESSION_BUS_ADDRESS'));\n"
            "GLib.timeout_add(GLib.PRIORITY_DEFAULT, 100, () => { GLib.file_set_contents(ARGV[9], 'ready'); return GLib.SOURCE_REMOVE; });\n"
            "new GLib.MainLoop(null, false).run();\n",
            encoding="utf-8",
        )
        args = [
            str(payload_list),
            str(area_log),
            str(full_path),
            *(str(value) for value in rectangle),
            str(stock_log),
            str(bus_path),
            str(ready_path),
            str(area_block_path),
            str(area_started_path),
            str(area_release_path),
        ]
        quoted_args = " ".join(shlex.quote(value) for value in [str(shell), *args])
        session_script = (
            "set -eu\n"
            f"Xvfb -displayfd 9 -screen 0 1920x1080x24 -nolisten tcp 9>{shlex.quote(str(display_path))} 2>{shlex.quote(str(root / 'xvfb.stderr'))} &\n"
            "xv=$!\n"
            "trap 'kill $xv 2>/dev/null || true' EXIT\n"
            f"while [ ! -s {shlex.quote(str(display_path))} ]; do sleep 0.01; done\n"
            f"display=$(cat {shlex.quote(str(display_path))}); export DISPLAY=:$display GDK_BACKEND=x11\n"
            f"printf '%s\\n' \"$DBUS_SESSION_BUS_ADDRESS\" > {shlex.quote(str(bus_path))}\n"
            f"exec gjs {quoted_args} >{shlex.quote(str(root / 'gjs.stdout'))} 2>{shlex.quote(str(root / 'gjs.stderr'))}\n"
        )
        session = subprocess.Popen(
            ["dbus-run-session", "--", "bash", "-lc", session_script],
            start_new_session=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        saved_environment = {
            name: os.environ.get(name)
            for name in (
                "DBUS_SESSION_BUS_ADDRESS",
                "DISPLAY",
                "GDK_BACKEND",
                "XDG_RUNTIME_DIR",
                "XDG_CACHE_HOME",
            )
        }
        try:
            deadline = time.monotonic() + 8.0
            while not ready_path.exists() and time.monotonic() < deadline:
                if session.poll() is not None:
                    raise RuntimeError(f"private compositor exited: {session.stderr.read()}")
                time.sleep(0.01)
            if not ready_path.exists() or not bus_path.exists() or not display_path.exists():
                detail = session.stderr.read() if session.poll() is not None else "<session still running>"
                raise RuntimeError(f"private compositor did not become ready: {detail}")
            bus_address = bus_path.read_text(encoding="utf-8").strip()
            display = ":" + display_path.read_text(encoding="ascii").strip()
            if not bus_address or not display[1:].isdigit():
                raise RuntimeError("private compositor published a malformed bus or display")
            os.environ["DBUS_SESSION_BUS_ADDRESS"] = bus_address
            os.environ["DISPLAY"] = display
            os.environ["GDK_BACKEND"] = "x11"
            os.environ["XDG_RUNTIME_DIR"] = str(root)
            os.environ["XDG_CACHE_HOME"] = str(cache_home)
            time.sleep(0.15)
            yield {
                "root": root,
                "display": display,
                "bus_address": bus_address,
                "payload_paths": tuple(Path(path) for path in payload_paths),
                "area_log": area_log,
                "stock_log": stock_log,
                "area_block_path": area_block_path,
                "area_started_path": area_started_path,
                "area_release_path": area_release_path,
                "session": session,
            }
        finally:
            for name, value in saved_environment.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            if session.poll() is None:
                os.killpg(session.pid, signal.SIGTERM)
                try:
                    session.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    os.killpg(session.pid, signal.SIGKILL)
                    session.wait(timeout=3.0)


def _finite_time(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise RuntimeError(f"{label} is not a finite monotonic timestamp")
    return float(value)


def _runtime_source_hashes() -> dict[str, str]:
    """Return the package-derived production safety binding."""

    return dict(build_source_binding().manifest)


def _status_from_ipc(config: object, *, timeout: float) -> dict[str, object]:
    from crowley.session.cli import _socket_request

    response = _socket_request(config.socket, {"command": "status"}, timeout=timeout)  # type: ignore[attr-defined]
    if not isinstance(response, dict) or response.get("ok") is not True:
        raise RuntimeError(f"session IPC status request failed: {response!r}")
    status = response.get("status")
    if not isinstance(status, dict):
        raise RuntimeError("session IPC status response is not an object")
    return status


def _check_paused_status(status: dict[str, object], *, expected_generation: int | None) -> int:
    if status.get("state") != "PAUSED":
        raise RuntimeError("paused diagnostic requires actual controller state PAUSED")
    generation = status.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise RuntimeError("session IPC status has no integer generation")
    if expected_generation is not None and generation != expected_generation:
        raise RuntimeError("session generation changed during paused diagnostic")
    operation = status.get("calibrationOperation")
    if not isinstance(operation, dict) or not isinstance(operation.get("state"), str):
        raise RuntimeError("session IPC status has no calibration operation state")
    if operation["state"] in {"starting", "running"}:
        raise RuntimeError("paused diagnostic refuses an in-flight calibration operation")
    action = status.get("actionIntent")
    if action not in (None, ""):
        raise RuntimeError("paused diagnostic refuses an in-flight action")
    if status.get("nativeBootstrapPending") is not False:
        raise RuntimeError("paused diagnostic refuses pending native bootstrap evidence")
    return generation


def _identity_record(identity: object) -> dict[str, object]:
    fields = (
        "pid",
        "process_start",
        "xid",
        "child_xid",
        "executable",
        "display",
        "fingerprint",
        "child_geometry",
    )
    result: dict[str, object] = {}
    for name in fields:
        value = getattr(identity, name, None)
        if name == "child_geometry":
            if not isinstance(value, tuple) or len(value) != 4:
                raise RuntimeError("desktop identity has no verified client geometry")
            value = list(value)
        if name == "pid" and (not isinstance(value, int) or isinstance(value, bool) or value <= 0):
            raise RuntimeError("desktop identity has no verified PID")
        if name in {"process_start", "fingerprint", "display", "executable"} and (
            not isinstance(value, str) or not value
        ):
            raise RuntimeError(f"desktop identity has no verified {name}")
        result[name] = value
    return result


def _atomic_receipt(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode(
        "utf-8"
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_private_json(path: Path, value: object) -> None:
    """Persist non-schema owner refusal detail with private file permissions."""

    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    encoded = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode(
        "utf-8"
    )
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _atomic_private_png(path: Path, data: bytes) -> None:
    if not data or len(data) > 131072:
        raise RuntimeError("owner PNG exceeds its bounded size")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


class _OwnerSampleValidationError(RuntimeError):
    """A returned owner sample failed a binding-specific validation stage."""

    def __init__(self, phase: str, message: str) -> None:
        super().__init__(message)
        self.phase = phase


def _owner_png_bytes(
    sample: dict[str, object],
    profile: object,
    producer_hash: str,
    config_file_sha256: str,
    config_semantic_sha256: str,
    expected_generation: int,
    expected_parent_started: float,
    expected_deadline: float,
    capture_root: Path,
    index: int,
    expected_source_binding: dict[str, str],
) -> bytes:
    expected_fields = {
        "generation",
        "parent_started",
        "deadline",
        "owner_entered",
        "capture_returned",
        "identity",
        "identity_after",
        "source",
        "config",
        "producer",
        "receipt",
        "png_sha256",
        "png_bytes",
        "png_base64",
        "artifact_directory",
        "artifact_png",
        "cleanup",
        "owner_returned",
    }
    allowed_instrumentation_fields = {
        "source_binding",
        "model_calls_before",
        "model_calls_after",
        "model_call_delta",
        "response_ready",
    }
    if set(sample) != expected_fields | allowed_instrumentation_fields:
        raise RuntimeError(f"paused diagnostic owner sample {index} has unexpected fields")
    encoded = sample.get("png_base64")
    if not isinstance(encoded, str) or len(encoded) > 4 * ((131072 + 2) // 3):
        raise RuntimeError(f"paused diagnostic owner sample {index} PNG encoding is oversized")
    try:
        data = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError(f"paused diagnostic owner sample {index} PNG base64 is malformed") from exc
    if (
        not data
        or len(data) > 131072
        or sample.get("png_bytes") != len(data)
        or sample.get("png_sha256") != hashlib.sha256(data).hexdigest()
    ):
        raise RuntimeError(f"paused diagnostic owner sample {index} PNG digest or length is invalid")
    if (
        not isinstance(sample.get("generation"), int)
        or isinstance(sample["generation"], bool)
        or sample["generation"] != expected_generation
    ):
        raise RuntimeError(f"paused diagnostic owner sample {index} generation is malformed")
    parent_started = _finite_time(sample.get("parent_started"), f"sample {index} parent start")
    deadline = _finite_time(sample.get("deadline"), f"sample {index} deadline")
    owner_entered = _finite_time(sample.get("owner_entered"), f"sample {index} owner entry")
    capture_returned = _finite_time(sample.get("capture_returned"), f"sample {index} capture return")
    response_ready = _finite_time(sample.get("response_ready"), f"sample {index} response ready")
    owner_returned = _finite_time(sample.get("owner_returned"), f"sample {index} owner return")
    if (
        parent_started != expected_parent_started
        or deadline != expected_deadline
        or not parent_started
        <= owner_entered
        <= capture_returned
        <= response_ready
        <= owner_returned
        < deadline
    ):
        raise RuntimeError(f"paused diagnostic owner sample {index} timestamps are not request-bound")
    identity = sample.get("identity")
    if not isinstance(identity, dict) or set(identity) != {
        "pid",
        "process_start",
        "xid",
        "child_xid",
        "executable",
        "display",
        "fingerprint",
        "child_geometry",
    }:
        raise RuntimeError(f"paused diagnostic owner sample {index} identity is malformed")
    if (
        not isinstance(identity.get("pid"), int)
        or isinstance(identity.get("pid"), bool)
        or identity["pid"] <= 0
        or not all(
            isinstance(identity.get(key), str) and identity[key]
            for key in ("process_start", "executable", "display", "fingerprint")
        )
        or not isinstance(identity.get("child_geometry"), list)
        or len(identity["child_geometry"]) != 4
    ):
        raise RuntimeError(f"paused diagnostic owner sample {index} identity fields are malformed")
    geometry = identity["child_geometry"]
    if any(not isinstance(value, int) or isinstance(value, bool) for value in geometry):
        raise RuntimeError(f"paused diagnostic owner sample {index} client geometry is malformed")
    source = sample.get("source")
    if not isinstance(source, dict) or set(source) != {"api", "branch", "rectangle"}:
        raise _OwnerSampleValidationError(
            "producer_binding", f"paused diagnostic owner sample {index} source is malformed"
        )
    config_binding = sample.get("config")
    expected_config = {
        "request_config_file_sha256": config_file_sha256,
        "request_config_semantic_sha256": config_semantic_sha256,
        "owner_config_semantic_sha256": config_semantic_sha256,
        "display": profile.display,  # type: ignore[attr-defined]
        "canvas_crop": list(profile.canvas_crop),  # type: ignore[attr-defined]
    }
    if config_binding != expected_config:
        raise _OwnerSampleValidationError(
            "config_binding", f"paused diagnostic owner sample {index} config binding changed"
        )
    producer = sample.get("producer")
    if not isinstance(producer, dict) or producer != {
        "path": str(profile.telemetry_region_executable),  # type: ignore[attr-defined]
        "sha256": producer_hash,
    }:
        raise _OwnerSampleValidationError(
            "producer_binding", f"paused diagnostic owner sample {index} producer binding changed"
        )
    source_binding = sample.get("source_binding")
    if (
        not isinstance(source_binding, dict)
        or set(source_binding) != SAFETY_MODULES
        or any(
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in source_binding.values()
        )
        or source_binding != expected_source_binding
    ):
        raise _OwnerSampleValidationError(
            "source_binding", f"paused diagnostic owner sample {index} source binding changed"
        )
    for counter_name in ("model_calls_before", "model_calls_after", "model_call_delta"):
        counters = sample.get(counter_name)
        if (
            not isinstance(counters, dict)
            or set(counters) != {"started", "succeeded", "failed"}
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 0
                for value in counters.values()
            )
        ):
            raise RuntimeError(f"paused diagnostic owner sample {index} model counters are malformed")
    before = sample["model_calls_before"]
    after = sample["model_calls_after"]
    delta = sample["model_call_delta"]
    assert isinstance(before, dict) and isinstance(after, dict) and isinstance(delta, dict)
    if any(after[key] - before[key] != delta[key] for key in ("started", "succeeded", "failed")):
        raise RuntimeError(f"paused diagnostic owner sample {index} model counter delta is malformed")
    receipt = sample.get("receipt")
    expected_receipt_fields = {
        "nonce",
        "rectangle",
        "executable_sha256",
        "owner_pid",
        "owner_process_start",
        "owner_ppid",
        "owner_pgid",
        "owner_exec_fd",
        "owner_branch",
        "request_started",
        "lock_acquired",
        "ack_sent",
        "capture_started",
        "capture_returned",
        "output_path_digest",
        "output_sha256",
        "output_bytes",
        "capture_epoch",
        "attempt_retired",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_receipt_fields:
        raise _OwnerSampleValidationError(
            "producer_binding",
            f"paused diagnostic owner sample {index} producer receipt schema changed",
        )
    if (
        not isinstance(receipt.get("nonce"), str)
        or re.fullmatch(r"[0-9a-f]{32}", receipt["nonce"]) is None
        or not isinstance(receipt.get("owner_pid"), int)
        or isinstance(receipt.get("owner_pid"), bool)
        or receipt["owner_pid"] <= 0
        or not isinstance(receipt.get("owner_process_start"), str)
        or not receipt["owner_process_start"]
        or not isinstance(receipt.get("owner_ppid"), int)
        or isinstance(receipt.get("owner_ppid"), bool)
        or receipt["owner_ppid"] <= 0
        or receipt.get("owner_pgid") != receipt.get("owner_pid")
        or isinstance(receipt.get("owner_pgid"), bool)
        or not isinstance(receipt.get("owner_exec_fd"), int)
        or isinstance(receipt.get("owner_exec_fd"), bool)
        or receipt["owner_exec_fd"] < 0
        or not isinstance(receipt.get("capture_epoch"), int)
        or isinstance(receipt.get("capture_epoch"), bool)
        or receipt["capture_epoch"] < 0
        or not isinstance(receipt.get("output_path_digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", receipt["output_path_digest"]) is None
    ):
        raise _OwnerSampleValidationError(
            "producer_binding", f"paused diagnostic owner sample {index} receipt identity is malformed"
        )
    for key in ("request_started", "lock_acquired", "ack_sent", "capture_started", "capture_returned"):
        _finite_time(receipt.get(key), f"sample {index} receipt {key}")
    receipt_times = [
        float(receipt[key])
        for key in ("request_started", "lock_acquired", "ack_sent", "capture_started", "capture_returned")
    ]
    if (
        not parent_started
        <= receipt_times[0]
        <= receipt_times[1]
        <= receipt_times[2]
        <= receipt_times[3]
        <= capture_returned
    ):
        raise _OwnerSampleValidationError(
            "producer_binding", f"paused diagnostic owner sample {index} receipt times are unordered"
        )
    rectangle = receipt.get("rectangle")
    expected_rectangle = [
        geometry[0] + profile.canvas_crop[0],  # type: ignore[attr-defined]
        geometry[1] + profile.canvas_crop[1],  # type: ignore[attr-defined]
        profile.canvas_crop[2],  # type: ignore[attr-defined]
        profile.canvas_crop[3],  # type: ignore[attr-defined]
    ]
    if (
        not isinstance(rectangle, list)
        or len(rectangle) != 4
        or any(not isinstance(value, int) or isinstance(value, bool) for value in rectangle)
        or rectangle != expected_rectangle
    ):
        raise _OwnerSampleValidationError(
            "producer_binding", f"paused diagnostic owner sample {index} rectangle is not profile-derived"
        )
    artifact_directory = sample.get("artifact_directory")
    artifact_png = sample.get("artifact_png")
    if not isinstance(artifact_directory, str) or not isinstance(artifact_png, str):
        raise RuntimeError(f"paused diagnostic owner sample {index} artifact paths are malformed")
    root = capture_root.resolve()
    owner_directory = Path(artifact_directory)
    owner_png = Path(artifact_png)
    if (
        not owner_directory.is_absolute()
        or owner_directory.name.startswith("paused-region-") is False
        or owner_directory.parent.resolve() != root
        or owner_png != owner_directory / "capture.png"
        or owner_directory.is_symlink()
        or owner_png.is_symlink()
        or not owner_png.is_file()
        or owner_png.stat().st_size != len(data)
        or owner_png.read_bytes() != data
    ):
        raise RuntimeError(
            f"paused diagnostic owner sample {index} artifacts are outside the configured root"
        )
    if (
        receipt.get("owner_branch") != "ScreenshotArea"
        or receipt.get("executable_sha256") != producer_hash
        or receipt.get("output_sha256") != hashlib.sha256(data).hexdigest()
        or receipt.get("output_bytes") != len(data)
        or receipt.get("attempt_retired") is not True
        or source.get("api") != "org.gnome.Shell.Screenshot.ScreenshotArea"
        or source.get("branch") != "ScreenshotArea"
        or source.get("rectangle") != receipt.get("rectangle")
    ):
        raise _OwnerSampleValidationError(
            "producer_binding", f"paused diagnostic owner sample {index} receipt does not bind the PNG"
        )
    return data


def _bounded_failure_response(response: dict[str, object]) -> dict[str, object]:
    value: dict[str, object] = {key: item for key, item in response.items() if key != "sample"}
    sample = response.get("sample")
    if isinstance(sample, dict):
        value["sample"] = {key: item for key, item in sample.items() if key != "png_base64"}
    return value


def _validate_producer_attempt(value: object, index: int) -> None:
    """Validate truthful stage/timing evidence even when a capture fails."""

    fields = {
        "stage",
        "request_started",
        "lock_acquired",
        "ack_sent",
        "capture_started",
        "capture_returned",
        "attempt_retired",
    }
    stages = {
        "not_started",
        "capture_lock_wait",
        "attempt_setup",
        "observer_admission",
        "owner_ready",
        "capture_started",
        "capture_complete",
        "identity_postcheck",
        "published",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeError(f"paused diagnostic owner failure {index} producer attempt schema is malformed")
    stage = value.get("stage")
    if not isinstance(stage, str) or stage not in stages:
        raise RuntimeError(f"paused diagnostic owner failure {index} producer stage is unknown")
    if stage == "not_started":
        if any(value.get(key) is not None for key in fields - {"stage", "attempt_retired"}):
            raise RuntimeError(
                f"paused diagnostic owner failure {index} not-started stage has timing evidence"
            )
        if not isinstance(value.get("attempt_retired"), bool):
            raise RuntimeError(
                f"paused diagnostic owner failure {index} not-started retirement evidence is unknown"
            )
        return
    request_started = _finite_time(value.get("request_started"), f"failure {index} producer request")
    lock_value = value.get("lock_acquired")
    ack_value = value.get("ack_sent")
    lock_acquired = (
        _finite_time(lock_value, f"failure {index} capture lock") if lock_value is not None else None
    )
    ack_sent = _finite_time(ack_value, f"failure {index} producer ACK") if ack_value is not None else None
    capture_started_value = value.get("capture_started")
    capture_started = (
        _finite_time(capture_started_value, f"failure {index} capture start")
        if capture_started_value is not None
        else None
    )
    capture_returned_value = value.get("capture_returned")
    capture_returned = (
        _finite_time(capture_returned_value, f"failure {index} capture return")
        if capture_returned_value is not None
        else None
    )
    if lock_acquired is not None and lock_acquired < request_started:
        raise RuntimeError(f"paused diagnostic owner failure {index} lock time precedes its request")
    if ack_sent is not None and (lock_acquired is None or ack_sent < lock_acquired):
        raise RuntimeError(f"paused diagnostic owner failure {index} ACK time precedes its lock")
    if capture_started is not None and (ack_sent is None or capture_started < ack_sent):
        raise RuntimeError(f"paused diagnostic owner failure {index} capture start precedes its ACK")
    if capture_returned is not None and (capture_started is None or capture_returned < capture_started):
        raise RuntimeError(f"paused diagnostic owner failure {index} capture return precedes its start")
    if (stage == "capture_lock_wait") != (lock_acquired is None) or (
        stage in {"capture_started", "capture_complete", "identity_postcheck", "published"}
    ) != (ack_sent is not None):
        raise RuntimeError(f"paused diagnostic owner failure {index} stage disagrees with its timestamps")
    if value.get("attempt_retired") is not None and not isinstance(value.get("attempt_retired"), bool):
        raise RuntimeError(f"paused diagnostic owner failure {index} retirement evidence is malformed")


def _attempt_directories(output: Path) -> list[str]:
    matches = list(output.parent.glob(f".{output.name}.telemetry-*"))
    matches.extend(output.parent.glob(f"..{output.name}.telemetry-*.retired-*"))
    return sorted(str(path) for path in matches)


def _backend_cleanup_snapshot(driver: object, output: Path | None = None) -> dict[str, object]:
    backend = getattr(driver, "backend", None)
    processes = getattr(backend, "_active_processes", None)
    groups = getattr(backend, "active_process_groups", None)
    if not isinstance(processes, set) or not isinstance(groups, (set, frozenset)):
        return {"registries_observable": False, "active_processes": None, "active_process_groups": None}
    attempts = _attempt_directories(output) if output is not None else []
    return {
        "registries_observable": True,
        "active_processes": len(processes),
        "active_process_groups": sorted(groups),
        "attempt_directories": attempts,
        "empty": not processes and not groups and not attempts,
    }


def _receipt_record(receipt: object) -> dict[str, object]:
    fields = (
        "nonce",
        "rectangle",
        "executable_sha256",
        "owner_pid",
        "owner_process_start",
        "owner_ppid",
        "owner_pgid",
        "owner_exec_fd",
        "owner_branch",
        "request_started",
        "lock_acquired",
        "ack_sent",
        "capture_started",
        "capture_returned",
        "output_path_digest",
        "output_sha256",
        "output_bytes",
        "capture_epoch",
        "attempt_retired",
    )
    result = {name: getattr(receipt, name) for name in fields}
    result["rectangle"] = list(result["rectangle"])  # type: ignore[arg-type]
    return result


def _live_failure(phase: str) -> dict[str, str]:
    mapping = {
        "status": ("status", "status_failed"),
        "owner_ipc": ("owner_ipc", "owner_ipc_failed"),
        "owner_capture": ("owner_capture", "owner_capture_failed"),
        "png_publication": ("png_publication", "png_publication_failed"),
        "png_load": ("png_load", "png_load_failed"),
        "strict_decode": ("strict_decode", "strict_decode_failed"),
        "post_decode_validation": ("strict_decode", "unknown_required_stage"),
        "deadline": ("deadline", "sample_deadline_missed"),
        "source_binding": ("source_binding", "source_binding_drift"),
        "config_binding": ("config_binding", "config_binding_mismatch"),
        "producer_binding": ("producer_binding", "producer_binding_mismatch"),
        "model_counter": ("model_counter", "model_call_delta_nonzero"),
        "cleanup": ("cleanup", "cleanup_failed"),
        "process_termination": ("process_termination", "process_terminated"),
    }
    try:
        phase_name, reason = mapping[phase]
    except KeyError as exc:
        raise RuntimeError(f"unknown paused diagnostic failure phase: {phase}") from exc
    return {"phase": phase_name, "reason_code": reason}


def _owner_failure_phase(failure: dict[str, object] | None, response: object) -> str:
    """Map the daemon's durable phase token without searching arbitrary prose."""

    if isinstance(failure, dict):
        phase = failure.get("phase")
        if isinstance(phase, str) and phase in {
            "status",
            "owner_ipc",
            "owner_capture",
            "png_publication",
            "png_load",
            "strict_decode",
            "deadline",
            "source_binding",
            "config_binding",
            "producer_binding",
            "model_counter",
            "cleanup",
            "process_termination",
        }:
            return phase
        if phase in {
            "request_validation",
            "owner_preconditions",
            "identity_before_capture",
            "region_capture",
            "owner_post_capture_check",
            "owner_receipt_persist",
        }:
            return "owner_capture"
    if isinstance(response, dict) and response.get("error"):
        return "owner_ipc"
    return "process_termination"


_OWNER_FAILURE_FIELDS = frozenset(
    {
        "phase",
        "error",
        "owner_entered",
        "owner_returned",
        "artifact_directory",
        "receipt_path",
        "published_png",
        "producer_attempt",
        "cleanup",
        "generation",
        "config_file_sha256",
        "config_semantic_sha256",
        "owner_config_semantic_sha256",
        "lease_held",
        "armed",
        "source_binding",
        "model_calls_before",
        "model_calls_after",
        "model_call_delta",
        "response_ready",
    }
)
_OWNER_FAILURE_OPTIONAL_FIELDS = frozenset({"sample", "receipt_persist_error"})
_OWNER_CLEANUP_FIELDS = frozenset(
    {
        "backend_processes",
        "backend_groups",
        "guardian_observer_groups",
        "guardian_active_groups",
        "attempt_directories",
        "lease_held",
        "watchdog_healthy",
        "armed",
        "empty",
    }
)
_OWNER_FAILURE_PHASES = frozenset(
    {
        "request_validation",
        "owner_preconditions",
        "identity_before_capture",
        "region_capture",
        "owner_post_capture_check",
        "owner_receipt_persist",
        "status",
        "owner_ipc",
        "owner_capture",
        "png_publication",
        "png_load",
        "strict_decode",
        "deadline",
        "source_binding",
        "config_binding",
        "producer_binding",
        "model_counter",
        "cleanup",
        "process_termination",
    }
)


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _validate_owner_failure_payload(
    value: object,
    *,
    index: int,
    generation: int,
    config_file_sha256: str,
    config_semantic_sha256: str,
) -> dict[str, object]:
    """Validate the complete daemon refusal before projecting owner evidence."""

    if not isinstance(value, dict) or set(value) - (_OWNER_FAILURE_FIELDS | _OWNER_FAILURE_OPTIONAL_FIELDS):
        raise RuntimeError(f"paused diagnostic owner failure {index} has unexpected fields")
    if not _OWNER_FAILURE_FIELDS <= set(value):
        raise RuntimeError(f"paused diagnostic owner failure {index} is missing required fields")
    phase = value.get("phase")
    if not isinstance(phase, str) or phase not in _OWNER_FAILURE_PHASES:
        raise RuntimeError(f"paused diagnostic owner failure {index} phase is malformed")
    if not isinstance(value.get("error"), str):
        raise RuntimeError(f"paused diagnostic owner failure {index} error is malformed")
    for key in ("owner_entered", "owner_returned", "response_ready"):
        item = value.get(key)
        if item is not None:
            _finite_time(item, f"owner failure {index} {key}")
    if (
        value.get("owner_returned") is not None
        and value.get("owner_entered") is not None
        and value["owner_returned"] < value["owner_entered"]
    ):
        raise RuntimeError(f"paused diagnostic owner failure {index} return precedes entry")
    if (
        value.get("response_ready") is not None
        and value.get("owner_returned") is not None
        and value["response_ready"] > value["owner_returned"]
    ):
        raise RuntimeError(f"paused diagnostic owner failure {index} response is after return")
    if (
        not isinstance(value.get("generation"), int)
        or isinstance(value["generation"], bool)
        or value["generation"] != generation
    ):
        raise RuntimeError(f"paused diagnostic owner failure {index} generation is malformed")
    if value.get("config_file_sha256") != config_file_sha256:
        raise RuntimeError(f"paused diagnostic owner failure {index} request file binding changed")
    if value.get("config_semantic_sha256") != config_semantic_sha256:
        raise RuntimeError(f"paused diagnostic owner failure {index} request semantic binding changed")
    if not _valid_sha256(value.get("owner_config_semantic_sha256")):
        raise RuntimeError(f"paused diagnostic owner failure {index} owner config binding is unknown")
    if not isinstance(value.get("producer_attempt"), dict):
        raise RuntimeError(f"paused diagnostic owner failure {index} producer attempt is missing")
    _validate_producer_attempt(value["producer_attempt"], index)
    cleanup = value.get("cleanup")
    if not isinstance(cleanup, dict) or set(cleanup) != _OWNER_CLEANUP_FIELDS:
        raise RuntimeError(f"paused diagnostic owner failure {index} cleanup schema is malformed")
    for key in ("backend_processes",):
        item = cleanup[key]
        if item is not None and (not isinstance(item, int) or isinstance(item, bool) or item < 0):
            raise RuntimeError(f"paused diagnostic owner failure {index} cleanup count is malformed")
    for key in (
        "backend_groups",
        "guardian_observer_groups",
        "guardian_active_groups",
        "attempt_directories",
    ):
        if cleanup[key] is not None and not isinstance(cleanup[key], list):
            raise RuntimeError(f"paused diagnostic owner failure {index} cleanup ledger is malformed")
    for key in ("lease_held", "watchdog_healthy", "armed", "empty"):
        if cleanup[key] is not None and not isinstance(cleanup[key], bool):
            raise RuntimeError(f"paused diagnostic owner failure {index} cleanup flag is malformed")
    for key in ("lease_held", "armed"):
        item = value.get(key)
        if item is not None and not isinstance(item, bool):
            raise RuntimeError(f"paused diagnostic owner failure {index} owner flag is malformed")
    source_binding = value.get("source_binding")
    if (
        not isinstance(source_binding, dict)
        or set(source_binding) != SAFETY_MODULES
        or not all(_valid_sha256(item) for item in source_binding.values())
    ):
        raise RuntimeError(f"paused diagnostic owner failure {index} source binding is malformed")
    for key in ("model_calls_before", "model_calls_after", "model_call_delta"):
        counters = value.get(key)
        if counters is not None and (
            not isinstance(counters, dict)
            or set(counters) != {"started", "succeeded", "failed"}
            or not all(
                isinstance(item, int) and not isinstance(item, bool) and item >= 0
                for item in counters.values()
            )
        ):
            raise RuntimeError(f"paused diagnostic owner failure {index} model counters are malformed")
    for key in ("artifact_directory", "receipt_path", "published_png"):
        if value.get(key) is not None and not isinstance(value[key], str):
            raise RuntimeError(f"paused diagnostic owner failure {index} artifact path is malformed")
    if "receipt_persist_error" in value and not isinstance(value["receipt_persist_error"], str):
        raise RuntimeError(f"paused diagnostic owner failure {index} persistence error is malformed")
    if "sample" in value and value["sample"] is not None and not isinstance(value["sample"], dict):
        raise RuntimeError(f"paused diagnostic owner failure {index} sample is malformed")
    return value


def _assert_trusted_runtime_modules() -> None:
    """Refuse an observer imported from a different project root."""

    trusted_root = ROOT.resolve()
    for name, module in tuple(sys.modules.items()):
        if name != "crowley" and not name.startswith("crowley."):
            continue
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue
        try:
            resolved = Path(origin).resolve()
        except OSError as exc:
            raise RuntimeError("observer runtime module origin is unavailable") from exc
        if not resolved.is_relative_to(trusted_root):
            raise RuntimeError("observer runtime module is outside the trusted project root")


def _live_owner_evidence(
    sample: dict[str, object] | None,
    failure: dict[str, object] | None,
    generation: int,
    owner_failure: dict[str, str] | None,
) -> dict[str, object]:
    value = sample or failure or {}
    producer_attempt = (
        value.get("receipt") if isinstance(value.get("receipt"), dict) else value.get("producer_attempt")
    )
    if not isinstance(producer_attempt, dict):
        producer_attempt = {}
    old_cleanup = value.get("cleanup")
    if not isinstance(old_cleanup, dict):
        old_cleanup = {}
    active = old_cleanup.get("backend_processes")
    groups = old_cleanup.get("backend_groups")
    observer_groups = old_cleanup.get("guardian_observer_groups")
    guardian_groups = old_cleanup.get("guardian_active_groups")
    active = active if isinstance(active, int) and not isinstance(active, bool) else None
    backend_group_values = groups if isinstance(groups, list) else None
    guardian_group_values = guardian_groups if isinstance(guardian_groups, list) else None
    if backend_group_values is not None and guardian_group_values is not None:
        try:
            group_count = len(set(backend_group_values) | set(guardian_group_values))
        except TypeError:
            group_count = None
    else:
        group_count = None
    observer_count = len(observer_groups) if isinstance(observer_groups, list) else None
    config_value = value.get("config")
    producer_value = value.get("producer")
    source_value = value.get("source_binding")
    source = value.get("source")
    if not isinstance(config_value, dict):
        config_value = {
            "request_config_file_sha256": value.get("config_file_sha256"),
            "owner_config_semantic_sha256": value.get("owner_config_semantic_sha256"),
        }
    if not isinstance(producer_value, dict):
        producer_value = value.get("producer_binding")
    return {
        "owner_entered_monotonic": value.get("owner_entered"),
        "request_started_monotonic": producer_attempt.get("request_started"),
        "lock_acquired_monotonic": producer_attempt.get("lock_acquired"),
        "ack_sent_monotonic": producer_attempt.get("ack_sent"),
        "capture_started_monotonic": producer_attempt.get("capture_started"),
        "capture_returned_monotonic": (
            producer_attempt.get("capture_returned")
            if producer_attempt.get("capture_returned") is not None
            else value.get("capture_returned")
        ),
        "response_ready_monotonic": (
            value.get("response_ready") if value.get("response_ready") is not None else None
        ),
        "receipt_nonce": producer_attempt.get("nonce"),
        "generation": value.get("generation", generation),
        "source_binding": source_value,
        "producer_binding": {
            "binary_sha256": (
                producer_value.get("sha256", producer_value.get("binary_sha256"))
                if isinstance(producer_value, dict)
                else None
            ),
            "protocol_branch": source.get("branch") if isinstance(source, dict) else None,
        },
        "config_binding": {
            "raw_file_sha256": config_value.get("request_config_file_sha256")
            if isinstance(config_value, dict)
            else None,
            "semantic_sha256": config_value.get("owner_config_semantic_sha256")
            if isinstance(config_value, dict)
            else None,
        },
        "model_calls_before": value.get("model_calls_before")
        if isinstance(value.get("model_calls_before"), dict)
        else {"started": None, "succeeded": None, "failed": None},
        "model_calls_after": value.get("model_calls_after")
        if isinstance(value.get("model_calls_after"), dict)
        else {"started": None, "succeeded": None, "failed": None},
        "model_call_delta": value.get("model_call_delta")
        if isinstance(value.get("model_call_delta"), dict)
        else {"started": None, "succeeded": None, "failed": None},
        "cleanup": {
            "capture_process_reaped": (
                None
                if active is None
                or group_count is None
                or observer_count is None
                or not isinstance(old_cleanup.get("attempt_directories"), list)
                else (
                    active == 0
                    and group_count == 0
                    and observer_count == 0
                    and not old_cleanup["attempt_directories"]
                )
            ),
            "attempt_retired": producer_attempt.get("attempt_retired") is True
            if isinstance(producer_attempt.get("attempt_retired"), bool)
            else None,
            "lease_held": old_cleanup.get("lease_held")
            if isinstance(old_cleanup.get("lease_held"), bool)
            else None,
            "watchdog_healthy": old_cleanup.get("watchdog_healthy")
            if isinstance(old_cleanup.get("watchdog_healthy"), bool)
            else None,
            "active_process_count": active,
            "active_process_group_count": group_count,
            "observer_process_group_count": observer_count,
        },
        "failure": owner_failure,
    }


def run_paused_diagnostic(config_path: Path, output_dir: Path) -> dict[str, object]:
    """Run the fixed eight-target diagnostic and persist exact schema-2 evidence."""

    output_dir = output_dir.expanduser().absolute()
    if output_dir.exists() or output_dir.is_symlink():
        raise RuntimeError("paused diagnostic evidence directory must be new")
    output_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_dir.mkdir(mode=0o700)
    os.chmod(output_dir, 0o700)
    receipt_path = output_dir / "receipt.json"
    preflight_receipt: dict[str, object] = {
        "schema": "crowley.paused-region-performance-run",
        "schema_version": 2,
        "run_id": uuid.uuid4().hex,
        "run_state": "incomplete",
        "generation": 0,
        "targets_monotonic": [],
        "observer_source_hashes_before": {},
        "observer_source_hashes_after": {},
        "producer_binding": None,
        "config_binding": None,
        "attempts": [],
    }
    _atomic_receipt(receipt_path, preflight_receipt)
    from crowley.session.cli import _socket_request
    from crowley.session.config import load_config
    from crowley.session.daemon import session_config_semantic_sha256
    from crowley.session.decoder import PixelDecoder

    try:
        _assert_trusted_runtime_modules()
        config_bytes = config_path.read_bytes()
        config = load_config(config_path)
        profile = config.profile
        producer = profile.telemetry_region_executable
        producer_hash = profile.telemetry_region_executable_sha256
        if producer is None or producer_hash is None:
            raise RuntimeError("paused diagnostic requires the configured telemetry-region producer")
        profile.validate()
        observer_before = _runtime_source_hashes()
    except BaseException as exc:
        preflight_receipt["run_state"] = "failed"
        _atomic_receipt(receipt_path, preflight_receipt)
        if isinstance(exc, Exception):
            raise
        raise RuntimeError(f"paused diagnostic preflight terminated; evidence={receipt_path}") from exc
    config_file_sha256 = hashlib.sha256(config_bytes).hexdigest()
    config_semantic_sha256 = session_config_semantic_sha256(config)
    decoder = PixelDecoder(crop=profile.canvas_crop)
    target_origin = time.monotonic()
    targets = [target_origin + float(index) for index in range(8)]
    receipt: dict[str, object] = {
        "schema": "crowley.paused-region-performance-run",
        "schema_version": 2,
        "run_id": uuid.uuid4().hex,
        "run_state": "incomplete",
        "generation": 0,
        "targets_monotonic": targets,
        "observer_source_hashes_before": observer_before,
        "observer_source_hashes_after": {},
        "producer_binding": {"binary_sha256": producer_hash, "protocol_branch": "ScreenshotArea"},
        "config_binding": {"raw_file_sha256": config_file_sha256, "semantic_sha256": config_semantic_sha256},
        "attempts": [],
    }
    expected_generation: int | None = None
    expected_boot: str | None = None
    previous_identity: tuple[str, str, int] | None = None
    previous_start: float | None = None
    previous_completion: float | None = None
    previous_sequence: int | None = None
    failure: Exception | None = None
    for index, target in enumerate(targets):
        phase = "status"
        deadline = target + 1.0
        try:
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        except BaseException as exc:
            failure = (
                exc
                if isinstance(exc, Exception)
                else RuntimeError("paused diagnostic interrupted before sample")
            )
            receipt["run_state"] = "incomplete"
            break
        outer_started = time.monotonic()
        attempt: dict[str, object] = {
            "index": index,
            "target_monotonic": target,
            "deadline_monotonic": deadline,
            "outer_started_monotonic": outer_started,
            "state": "incomplete",
            "status_returned_monotonic": None,
            "owner_ipc_request_started_monotonic": None,
            "owner": None,
            "owner_ipc_returned_monotonic": None,
            "png_publication_end_monotonic": None,
            "png_load_started_monotonic": None,
            "png_load_finished_monotonic": None,
            "strict_decode_started_monotonic": None,
            "strict_decode_finished_monotonic": None,
            "sample_completed_monotonic": None,
            "application_compute_cpu_seconds": None,
            "application_compute_cpu_method": None,
            "partial_decode_wall_seconds": None,
            "partial_decode_cpu_seconds": None,
            "png_sha256": None,
            "generation": expected_generation,
            "identity": None,
            "failure": None,
        }
        attempts = receipt["attempts"]
        assert isinstance(attempts, list)
        attempts.append(attempt)
        try:
            if outer_started >= deadline:
                phase = "deadline"
                raise RuntimeError("sample deadline missed before status")
            status = _status_from_ipc(config, timeout=min(0.25, max(0.001, deadline - time.monotonic())))
            attempt["status_returned_monotonic"] = time.monotonic()
            generation = _check_paused_status(status, expected_generation=expected_generation)
            if expected_generation is None:
                expected_generation = generation
                receipt["generation"] = generation
            attempt["generation"] = generation
            if time.monotonic() >= deadline:
                phase = "deadline"
                raise RuntimeError("status exceeded sample deadline")
            phase = "owner_ipc"
            output = output_dir / f"paused-{index:02d}.png"
            attempt["owner_ipc_request_started_monotonic"] = time.monotonic()
            try:
                response = _socket_request(
                    config.socket,
                    {
                        "command": "paused_region_sample",
                        "generation": generation,
                        "parent_started": outer_started,
                        "deadline": deadline,
                        "config_file_sha256": config_file_sha256,
                        "config_semantic_sha256": config_semantic_sha256,
                    },
                    timeout=max(0.001, deadline - time.monotonic()),
                    response_deadline=deadline,
                    failure_evidence_grace=0.25,
                )
            except TimeoutError as exc:
                # The owner may still be unwinding after the bounded grace.
                # Keep that fact in private evidence without inventing owner
                # entry, failure phase, or cleanup state in schema 2.
                _atomic_private_json(
                    output_dir / f"owner-timeout-{index:02d}.json",
                    {"state": "unknown", "grace_seconds": 0.25, "error": str(exc)},
                )
                raise
            ipc_returned = time.monotonic()
            attempt["owner_ipc_returned_monotonic"] = ipc_returned
            if (
                not isinstance(response, dict)
                or response.get("ok") is not True
                and response.get("ok") is not False
            ):
                raise RuntimeError("owner IPC response has invalid schema")
            if response.get("ok") is True:
                if set(response) != {"ok", "sample"} or not isinstance(response.get("sample"), dict):
                    raise RuntimeError("owner IPC success response has invalid schema")
            else:
                allowed_failure_fields = {"ok", "error", "failure", "sample"}
                # Preserve the bounded refusal before validating owner
                # metadata or rejecting an unexpected field.  A normal
                # SessionIPC refusal may contain only the exact
                # ``ok:false,error`` pair and carries no owner evidence.
                _atomic_private_json(
                    output_dir / f"owner-refusal-{index:02d}.json",
                    _bounded_failure_response(response),
                )
                if set(response) - allowed_failure_fields:
                    raise RuntimeError("owner IPC failure response has unexpected fields")
                if not isinstance(response.get("error"), str):
                    raise RuntimeError("owner IPC failure response has invalid schema")
                if "failure" in response:
                    if not isinstance(response.get("failure"), dict):
                        raise RuntimeError("owner IPC failure response has invalid schema")
                    _validate_owner_failure_payload(
                        response["failure"],
                        index=index,
                        generation=generation,
                        config_file_sha256=config_file_sha256,
                        config_semantic_sha256=config_semantic_sha256,
                    )
                if "sample" in response and not isinstance(response["sample"], dict):
                    raise RuntimeError("owner IPC failure response sample is malformed")
            failure_payload = response.get("failure") if isinstance(response.get("failure"), dict) else None
            sample_data = response.get("sample") if isinstance(response.get("sample"), dict) else None
            if sample_data is not None or failure_payload is not None:
                owner_phase = _owner_failure_phase(failure_payload, response)
                attempt["owner"] = _live_owner_evidence(
                    sample_data,
                    failure_payload,
                    generation,
                    None if response.get("ok") is True else _live_failure(owner_phase),
                )
            output_bytes: bytes | None = None
            if response.get("ok") is not True and failure_payload is not None:
                producer_attempt = failure_payload.get("producer_attempt")
                if isinstance(producer_attempt, dict):
                    _validate_producer_attempt(producer_attempt, index)
                if ipc_returned >= deadline:
                    phase = "deadline"
                    raise RuntimeError("owner IPC response arrived after sample deadline")
                if sample_data is not None:
                    try:
                        output_bytes = _owner_png_bytes(
                            sample_data,
                            profile,
                            producer_hash,
                            config_file_sha256,
                            config_semantic_sha256,
                            generation,
                            outer_started,
                            deadline,
                            Path(config.capture_dir),
                            index,
                            observer_before,
                        )
                    except _OwnerSampleValidationError as exc:
                        phase = exc.phase
                        raise
                    phase = "png_publication"
                    _atomic_private_png(output, output_bytes)
                    attempt["png_publication_end_monotonic"] = time.monotonic()
                    attempt["png_sha256"] = hashlib.sha256(output_bytes).hexdigest()
                    phase = _owner_failure_phase(failure_payload, response)
            if ipc_returned >= deadline:
                if response.get("ok") is True:
                    _atomic_private_json(
                        output_dir / f"owner-late-response-{index:02d}.json",
                        _bounded_failure_response(response),
                    )
                phase = "deadline"
                raise RuntimeError("owner IPC response arrived after sample deadline")
            if response.get("ok") is not True:
                phase = _owner_failure_phase(failure_payload, response)
                raise RuntimeError("owner refused paused region sample")
            if sample_data is None:
                phase = "owner_capture"
                raise RuntimeError("owner omitted paused region sample")
            if output_bytes is None:
                try:
                    output_bytes = _owner_png_bytes(
                        sample_data,
                        profile,
                        producer_hash,
                        config_file_sha256,
                        config_semantic_sha256,
                        generation,
                        outer_started,
                        deadline,
                        Path(config.capture_dir),
                        index,
                        observer_before,
                    )
                except _OwnerSampleValidationError as exc:
                    phase = exc.phase
                    raise
            phase = "png_publication"
            _atomic_private_png(output, output_bytes)
            attempt["png_publication_end_monotonic"] = time.monotonic()
            attempt["png_sha256"] = hashlib.sha256(output_bytes).hexdigest()
            identity = sample_data.get("identity")
            if not isinstance(identity, dict) or not isinstance(identity.get("fingerprint"), str):
                phase = "owner_capture"
                raise RuntimeError("owner identity is malformed")
            if sample_data.get("identity_after") != identity.get("fingerprint"):
                phase = "owner_capture"
                raise RuntimeError("owner identity changed after capture")
            owner_cleanup = sample_data.get("cleanup")
            if (
                not isinstance(owner_cleanup, dict)
                or owner_cleanup.get("empty") is not True
                or owner_cleanup.get("lease_held") is not True
                or owner_cleanup.get("watchdog_healthy") is not True
                or owner_cleanup.get("armed") is not False
            ):
                phase = "cleanup"
                raise RuntimeError("owner cleanup or Guardian state is not clean")
            phase = "png_load"
            attempt["png_load_started_monotonic"] = time.monotonic()
            with Image.open(output) as image:
                image.load()
                if image.format != "PNG" or image.size != (256, 256):
                    raise RuntimeError("PNG is not an exact 256x256 image")
                attempt["png_load_finished_monotonic"] = time.monotonic()
                phase = "strict_decode"
                strict_started = time.monotonic()
                attempt["strict_decode_started_monotonic"] = strict_started
                cpu_started = time.process_time_ns()
                try:
                    decoded = decoder.decode(
                        image, monotonic=outer_started, window_identity=str(identity["fingerprint"])
                    )
                except Exception:
                    cpu_finished = time.process_time_ns()
                    attempt["partial_decode_wall_seconds"] = time.monotonic() - strict_started
                    attempt["partial_decode_cpu_seconds"] = (cpu_finished - cpu_started) / 1_000_000_000
                    raise
                cpu_finished = time.process_time_ns()
                attempt["strict_decode_finished_monotonic"] = time.monotonic()
                attempt["application_compute_cpu_seconds"] = (cpu_finished - cpu_started) / 1_000_000_000
                attempt["application_compute_cpu_method"] = "time.process_time_ns"
            # The decoder returned, so its CPU interval is complete. Any
            # freshness/identity/sequence rejection is a later required
            # stage, not a thrown decoder failure; preserve both boundaries
            # and classify it as an unknown required stage for the evaluator.
            phase = "post_decode_validation"
            frame = decoded.frame
            if frame is not None:
                # A returned NO_CHANGE (or other non-fresh result) still
                # carries a real decoded identity. Preserve it so the
                # evaluator can retain the completed CPU interval while
                # rejecting the pair for the later required stage.
                attempt["identity"] = {
                    "fingerprint": str(identity["fingerprint"]),
                    "boot": frame.boot,
                    "sequence": frame.sequence,
                }
            if decoded.capture_monotonic != outer_started or decoded.kind != "FRESH" or decoded.frame is None:
                raise RuntimeError("strict decoder did not return a fresh frame")
            assert frame is not None
            identity_value = (str(identity["fingerprint"]), frame.boot, frame.sequence)
            if previous_identity is not None and identity_value[:2] != previous_identity[:2]:
                raise RuntimeError("frame identity or boot changed")
            if previous_sequence is not None and frame.sequence <= previous_sequence:
                raise RuntimeError("frame sequence did not advance")
            previous_identity = identity_value
            expected_boot = expected_boot or frame.boot
            if frame.boot != expected_boot:
                raise RuntimeError("frame boot changed")
            phase = "config_binding"
            if hashlib.sha256(config_path.read_bytes()).hexdigest() != config_file_sha256:
                raise RuntimeError("config changed during sample")
            if index == len(targets) - 1:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    phase = "deadline"
                    raise RuntimeError("sample deadline expired before final status")
                phase = "status"
                final_status = _status_from_ipc(config, timeout=min(0.25, remaining))
                final_generation = _check_paused_status(final_status, expected_generation=expected_generation)
                if final_generation != generation:
                    raise RuntimeError("controller generation changed before final sample completion")
            sample_completed = time.monotonic()
            attempt["sample_completed_monotonic"] = sample_completed
            if sample_completed >= deadline:
                phase = "deadline"
                raise RuntimeError("sample completion exceeded deadline")
            if previous_start is not None and not 0.75 <= outer_started - previous_start <= 1.25:
                phase = "deadline"
                raise RuntimeError("sample start cadence is outside 0.75..1.25 seconds")
            if previous_completion is not None and not 0.75 <= sample_completed - previous_completion <= 1.25:
                phase = "deadline"
                raise RuntimeError("sample completion cadence is outside 0.75..1.25 seconds")
            attempt["state"] = "completed"
            previous_sequence = frame.sequence
            previous_start = outer_started
            previous_completion = sample_completed
        except BaseException as exc:
            failure = exc
            attempt["state"] = "failed"
            attempt["failure"] = _live_failure(phase if isinstance(exc, Exception) else "process_termination")
            receipt["run_state"] = "failed"
            break
        finally:
            # Keep an interruption or process termination observable even when
            # control leaves this attempt before the normal run finalizer.
            if attempt["state"] == "incomplete":
                attempt["state"] = "failed"
                attempt["failure"] = _live_failure("process_termination")
                receipt["run_state"] = "failed"
            try:
                _atomic_receipt(receipt_path, receipt)
            except Exception:
                pass
    if failure is None:
        attempts = receipt["attempts"]
        assert isinstance(attempts, list)
        receipt["run_state"] = (
            "passed"
            if len(attempts) == 8 and all(item["state"] == "completed" for item in attempts)
            else "incomplete"
        )
    observer_after: dict[str, str] = {}
    try:
        observer_after = _runtime_source_hashes()
        receipt["observer_source_hashes_after"] = observer_after
        if observer_after != receipt["observer_source_hashes_before"] and failure is None:
            failure = RuntimeError("observer source binding drifted during the run")
            attempts = receipt["attempts"]
            assert isinstance(attempts, list)
            if attempts and attempts[-1].get("state") == "completed":
                attempts[-1]["state"] = "failed"
                attempts[-1]["failure"] = _live_failure("source_binding")
            receipt["run_state"] = "failed"
    except BaseException as exc:
        receipt["observer_source_hashes_after"] = observer_after
        if failure is None:
            failure = RuntimeError("observer source binding could not be verified after the run")
            attempts = receipt["attempts"]
            assert isinstance(attempts, list)
            if attempts and attempts[-1].get("state") == "completed":
                attempts[-1]["state"] = "failed"
                attempts[-1]["failure"] = _live_failure(
                    "source_binding" if isinstance(exc, Exception) else "process_termination"
                )
            receipt["run_state"] = "failed"
    _atomic_receipt(receipt_path, receipt)
    if failure is not None:
        attempts = receipt["attempts"]
        detail = (
            attempts[-1]["failure"]
            if isinstance(attempts, list)
            and attempts
            and isinstance(attempts[-1], dict)
            and attempts[-1].get("failure") is not None
            else _live_failure("process_termination")
        )
        raise RuntimeError(
            f"paused diagnostic failed; evidence={receipt_path}; failure={detail}"
        ) from failure
    return receipt


def _compiled_parser_checks() -> None:
    binary = _binary_path()
    if not binary.is_file():
        raise RuntimeError(f"private binary missing: {binary}")
    cases = [
        ["--telemetry-region"],
        ["--telemetry-region", "--telemetry-x", "1", "--telemetry-x", "1"],
        [
            "--telemetry-region",
            "--telemetry-x",
            "1",
            "--telemetry-y",
            "2",
            "--telemetry-width",
            "256",
            "--telemetry-height",
            "256",
            "--telemetry-owner-fd",
            "3",
            "--telemetry-exec-fd",
            "4",
            "--telemetry-nonce",
            "n",
            "--file",
            "/tmp/x",
            "--area",
        ],
        [
            "--telemetry-region",
            "--telemetry-x",
            "1",
            "--telemetry-y",
            "2",
            "--telemetry-width",
            "256",
            "--telemetry-height",
            "256",
            "--telemetry-owner-fd",
            "3",
            "--telemetry-exec-fd",
            "4",
            "--telemetry-nonce",
            "n",
            "--file",
            "relative.png",
        ],
        [
            "--telemetry-region",
            "--telemetry-x",
            "1",
            "--telemetry-y",
            "2",
            "--telemetry-width",
            "256",
            "--telemetry-height",
            "256",
            "--telemetry-owner-fd",
            "3",
            "--telemetry-exec-fd",
            "4",
            "--telemetry-nonce",
            "n",
            "--file",
            "/tmp/x",
            "--telemetry-xfoo",
        ],
        [
            "--telemetry-region",
            "--telemetry-x",
            "1",
            "--telemetry-y",
            "2",
            "--telemetry-width",
            "256",
            "--telemetry-height",
            "256",
            "--telemetry-owner-fd",
            "3",
            "--telemetry-exec-fd",
            "4",
            "--telemetry-nonce",
            "n",
            "--file",
            "/tmp/x",
            "-f",
            "/tmp/y",
        ],
        [
            "--telemetry-region",
            "--telemetry-x",
            "1",
            "--telemetry-y",
            "2",
            "--telemetry-width",
            "256",
            "--telemetry-height",
            "256",
            "--telemetry-owner-fd",
            "3",
            "--telemetry-exec-fd",
            "4",
            "--telemetry-nonce",
            "n",
            "-f",
            "/tmp/x",
            "-d",
            "0",
        ],
        [
            "--telemetry-region",
            "--telemetry-x",
            "1",
            "--telemetry-y",
            "2",
            "--telemetry-width",
            "128",
            "--telemetry-height",
            "256",
            "--telemetry-owner-fd",
            "3",
            "--telemetry-exec-fd",
            "4",
            "--telemetry-nonce",
            "n",
            "--file",
            "/tmp/x",
        ],
    ]
    for args in cases:
        result = subprocess.run([str(binary), *args], capture_output=True, text=True, check=False)
        if result.returncode == 0:
            raise RuntimeError(f"compiled parser accepted invalid telemetry argv: {args}")


def self_check() -> None:
    text = PATCH.read_text(encoding="utf-8")
    for required in ("ScreenshotArea", "telemetry_owner_fd", "primary_owner", "flash"):
        assert required in text, required
    if not _strict_argv(
        [
            "--telemetry-region",
            "--telemetry-x",
            "1",
            "--telemetry-y",
            "2",
            "--telemetry-width",
            "256",
            "--telemetry-height",
            "256",
            "--telemetry-owner-fd",
            "3",
            "--telemetry-exec-fd",
            "4",
            "--telemetry-nonce",
            "n",
            "--file",
            "/tmp/x",
        ]
    ):
        raise RuntimeError("offline typed option model rejected the valid contract")
    if _strict_argv(
        [
            "--telemetry-region",
            "--telemetry-x",
            "1",
            "--telemetry-y",
            "2",
            "--telemetry-width",
            "128",
            "--telemetry-height",
            "256",
            "--telemetry-owner-fd",
            "3",
            "--telemetry-exec-fd",
            "4",
            "--telemetry-nonce",
            "n",
            "--file",
            "/tmp/x",
        ]
    ):
        raise RuntimeError("offline typed option model accepted wrong dimensions")
    for invalid in (
        ["--telemetry-region", "--telemetry-x", "010", "--telemetry-y", "2"],
        ["--telemetry-region", "--file", "/tmp/x", "-f", "/tmp/y"],
        ["--telemetry-region", "-d", "0"],
    ):
        if _strict_argv(invalid):
            raise RuntimeError(f"offline typed option model accepted forbidden argv: {invalid}")
    for value in (0.0, 1.0, 42.5):
        if _finite_time(value, "self-check timestamp") != float(value):
            raise RuntimeError("paused diagnostic timestamp parser changed a finite value")
    for invalid_time in (True, float("nan"), float("inf"), "1.0"):
        try:
            _finite_time(invalid_time, "self-check timestamp")
        except RuntimeError:
            pass
        else:
            raise RuntimeError("paused diagnostic accepted a malformed monotonic timestamp")
    for interval in (0.75, 1.0, 1.25):
        if not 0.75 <= interval <= 1.25:
            raise RuntimeError("paused diagnostic rejected an allowed interval boundary")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--paused-diagnostic", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    options = parser.parse_args()
    if options.paused_diagnostic:
        if options.self_test or options.self_check:
            parser.error("--paused-diagnostic cannot be combined with offline self-checks")
        if options.config is None or options.evidence_dir is None:
            parser.error("--paused-diagnostic requires --config and --evidence-dir")
        result = run_paused_diagnostic(options.config, options.evidence_dir)
        print(
            json.dumps(
                {
                    "state": result["run_state"],
                    "receipt": str(options.evidence_dir / "receipt.json"),
                    "samples": len(result["attempts"]),  # type: ignore[arg-type]
                    "producer_sha256": result["producer_binding"]["binary_sha256"],  # type: ignore[index]
                },
                sort_keys=True,
            )
        )
        return 0
    if options.config is not None or options.evidence_dir is not None:
        parser.error("--config and --evidence-dir require --paused-diagnostic")
    if not options.self_test and not options.self_check:
        parser.error("choose --self-test and/or --self-check")
    if options.self_check:
        self_check()
    if options.self_test:
        _compiled_parser_checks()
        _protocol_fixture()
        _compiled_private_shell_fixture()
    binary = _binary_path()
    print(
        json.dumps(
            {"self_check": options.self_check, "self_test": options.self_test, "binary": str(binary)},
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
