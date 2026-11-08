from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from crowley.native import build as builder
from crowley.native import pins
from crowley.session.driver import DriverError, DriverProfile, X11Backend
from tests.test_session_input_tool import fixture_input_tool
from tests.x11_startup import start_private_xvfb

ROOT = Path(__file__).resolve().parents[1]
TOOLCHAIN = Path(os.environ.get("CROWLEY_RUST_TOOLCHAIN", ""))
SOURCE = builder.NATIVE_ROOT / "x11_identity_observer"


def _process_has_x_socket(pid: int) -> bool:
    """Return whether a live observer process has opened its X connection."""

    try:
        descriptors = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return False
    for descriptor in descriptors:
        try:
            if os.readlink(descriptor).startswith("socket:["):
                return True
        except OSError:
            continue
    return False


def _build(tmp_path: Path) -> Path:
    if not os.environ.get("CROWLEY_RUST_TOOLCHAIN"):
        pytest.fail("CROWLEY_RUST_TOOLCHAIN is required for native build tests")
    if not (TOOLCHAIN / "cargo").is_file() or not (TOOLCHAIN / "rustc").is_file():
        pytest.fail("CROWLEY_RUST_TOOLCHAIN must contain cargo and rustc")
    target = tmp_path / "target"
    env = {"RUSTC": str(TOOLCHAIN / "rustc"), "PATH": f"{TOOLCHAIN}:{os.defpath}", "LC_ALL": "C"}
    subprocess.run(
        [
            str(TOOLCHAIN / "cargo"),
            "build",
            "--release",
            "--locked",
            "--offline",
            "--target-dir",
            str(target),
        ],
        cwd=SOURCE,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return target / "release" / "x11_identity_observer"


def _request(**overrides: object) -> str:
    values: dict[str, object] = {
        "protocolVersion": 1,
        "display": ":nonexistent",
        "xauthority": "",
        "title": "World of Warcraft",
        "class": "wowb.exe",
        "executable": "WowB.exe",
        "winePrefix": "/tmp/wine",
        "loader": "wine-preloader",
        "outerWidth": 1928,
        "outerHeight": 1092,
        "clientWidth": 1920,
        "clientHeight": 1080,
        "nonce": "0123456789abcdef0123456789abcdef",
        "binaryDigest": "0" * 64,
    }
    values.update(overrides)
    return json.dumps(values, separators=(",", ":"))


def test_cargo_offline_build_and_binary_is_deterministic(tmp_path: Path) -> None:
    first = _build(tmp_path / "first")
    second = _build(tmp_path / "second")
    assert hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    assert first.stat().st_mode & 0o111


def test_hermetic_builder_publishes_binary_manifest(tmp_path: Path) -> None:
    output = tmp_path / "published" / "observer"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "crowley.native.build",
            "build",
            "observer",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    manifest = json.loads(result.stdout)
    published = output.parent
    binary = published / "current" / output.name
    manifest_path = published / "current" / output.with_suffix(".manifest.json").name
    assert binary.is_file() and binary.stat().st_mode & 0o111
    assert manifest_path.stat().st_mode & 0o777 == 0o600
    assert manifest["binary_sha256"] == hashlib.sha256(binary.read_bytes()).hexdigest()
    assert set(manifest["source_sha256"]) == {
        "native/x11_identity_observer/Cargo.toml",
        "native/x11_identity_observer/Cargo.lock",
        "native/x11_identity_observer/src/main.rs",
    }
    assert len(manifest["static_inputs"]) == 4
    driver = manifest["linker"]["driver"]
    assert driver["name"] == "cc"
    assert driver["version"]
    assert len(driver["sha256"]) == 64
    linker = manifest["linker"]["ld"]
    assert linker["name"] == "ld"
    assert linker["version"].startswith("GNU ld")
    assert len(linker["sha256"]) == 64


def test_hermetic_builder_rejects_unreviewed_source_inputs(tmp_path: Path) -> None:
    source = tmp_path / "observer-source"
    (source / "src").mkdir(parents=True)
    for relative in ("Cargo.toml", "Cargo.lock", "src/main.rs"):
        path = source / relative
        path.write_text("reviewed\n", encoding="utf-8")
    builder._validate_source_tree(source)
    (source / "build.rs").write_text("fn main() {}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="reviewed three-file set"):
        builder._validate_source_tree(source)
    (source / "build.rs").unlink()
    (source / ".cargo").mkdir()
    with pytest.raises(RuntimeError, match="reviewed layout"):
        builder._validate_source_tree(source)
    (source / ".cargo").rmdir()
    (source / "src" / "extra.rs").write_text("\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="reviewed three-file set"):
        builder._validate_source_tree(source)
    (source / "src" / "extra.rs").unlink()
    (source / "src" / "link.rs").symlink_to(source / "src" / "main.rs")
    with pytest.raises(RuntimeError, match="symlink"):
        builder._validate_source_tree(source)


def test_hermetic_builder_rejects_ambient_ancestor_cargo_configuration(tmp_path: Path) -> None:
    source = tmp_path / "project" / "native" / "x11_identity_observer"
    (source / "src").mkdir(parents=True)
    for relative in ("Cargo.toml", "Cargo.lock", "src/main.rs"):
        (source / relative).write_text("reviewed\n", encoding="utf-8")
    (source.parents[2] / ".cargo").mkdir()
    (source.parents[2] / ".cargo" / "config.toml").write_text(
        "[build]\ntarget-dir = '/tmp/ambient'\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="ancestor Cargo config"):
        builder._refuse_ancestor_build_inputs(source.parent)


@pytest.mark.parametrize("config_name", ["config", "config.toml"])
def test_hermetic_build_rejects_configuration_above_actual_build_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_name: str
) -> None:
    build_root = tmp_path / "private-cargo-build"
    source = build_root / "source-a"
    source.mkdir(parents=True)
    (build_root / ".cargo").mkdir()
    (build_root / ".cargo" / config_name).write_text(
        "[build]\ntarget-dir = '/tmp/ambient'\n", encoding="utf-8"
    )
    called = False

    def unexpected_cargo(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("Cargo must not run with ambient configuration")

    monkeypatch.setattr(builder.subprocess, "run", unexpected_cargo)
    with pytest.raises(RuntimeError, match="ancestor Cargo config"):
        builder._refuse_ancestor_build_inputs(source)
    assert not called


def test_private_publication_refuses_different_bytes_and_sets_mode_before_replace(tmp_path: Path) -> None:
    output = tmp_path / "published" / "observer"
    output.parent.mkdir(mode=0o700)
    builder._atomic_publish(output, b"first", 0o700)
    assert output.read_bytes() == b"first"
    assert output.stat().st_mode & 0o777 == 0o700
    with pytest.raises(RuntimeError, match="different published artifact"):
        builder._atomic_publish(output, b"second", 0o700)
    manifest = output.with_suffix(".manifest.json")
    builder._atomic_publish(manifest, b"manifest", 0o600)
    assert manifest.stat().st_mode & 0o777 == 0o600


def test_observer_refuses_malformed_request_before_display(tmp_path: Path) -> None:
    binary = _build(tmp_path)
    result = subprocess.run([str(binary)], input=b'{"display":":0"}\n', capture_output=True, check=False)
    assert result.returncode != 0
    assert json.loads(result.stdout) == {
        "protocolVersion": 1,
        "error": "request keys are not canonical",
    }


def test_observer_requires_protocol_version_one(tmp_path: Path) -> None:
    binary = _build(tmp_path)
    result = subprocess.run(
        [str(binary)], input=(_request(protocolVersion=2) + "\n").encode(), capture_output=True, check=False
    )
    assert result.returncode != 0
    assert json.loads(result.stdout) == {"protocolVersion": 1, "error": "unsupported protocolVersion"}


@pytest.mark.parametrize(
    "request_suffix",
    [
        " trailing",
        ',"display":":1"}',
    ],
)
def test_observer_refuses_trailing_or_duplicate_request_data(tmp_path: Path, request_suffix: str) -> None:
    binary = _build(tmp_path)
    request = _request()
    payload = (
        request + request_suffix if request_suffix.startswith(" trailing") else request[:-1] + request_suffix
    )
    result = subprocess.run([str(binary)], input=(payload + "\n").encode(), capture_output=True, check=False)
    assert result.returncode != 0
    assert json.loads(result.stdout).keys() == {"protocolVersion", "error"}


@pytest.mark.parametrize(
    "malformed",
    [
        lambda request: request[:-1] + ',"unknown":1}',
        lambda request: request.replace('"display":":nonexistent"', '"display":"\\q"'),
        lambda request: request.replace('"outerWidth":1928', '"outerWidth":1928.0'),
        lambda request: request.replace('"outerWidth":1928', '"outerWidth":01'),
        lambda request: request[:-1] + " true}",
    ],
)
def test_observer_refuses_noncanonical_json_request(tmp_path: Path, malformed) -> None:
    binary = _build(tmp_path)
    result = subprocess.run(
        [str(binary)], input=(malformed(_request()) + "\n").encode(), capture_output=True, check=False
    )
    assert result.returncode != 0
    assert json.loads(result.stdout).keys() == {"protocolVersion", "error"}


def test_observer_refuses_bad_process_without_fallback(tmp_path: Path) -> None:
    binary = _build(tmp_path)
    result = subprocess.run(
        [str(binary)], input=(_request() + "\n").encode(), capture_output=True, check=False
    )
    assert result.returncode != 0
    response = json.loads(result.stdout)
    assert set(response) == {"protocolVersion", "error"}
    assert response["protocolVersion"] == 1
    assert b"fallback" not in result.stderr.lower()


@pytest.mark.parametrize("mutation", ["nonce", "binaryDigest", "loader", "clientWidth"])
def test_protocol_inputs_are_bounded_and_explicit(tmp_path: Path, mutation: str) -> None:
    binary = _build(tmp_path)
    value = "bad" if mutation in {"nonce", "binaryDigest", "loader"} else 1
    result = subprocess.run(
        [str(binary)], input=(_request(**{mutation: value}) + "\n").encode(), capture_output=True, check=False
    )
    assert result.returncode != 0
    assert json.loads(result.stdout).keys() == {"protocolVersion", "error"}


@pytest.mark.skipif(
    shutil.which("Xvfb") is None or shutil.which("cc") is None,
    reason="owned Xvfb/X11 fixture toolchain is unavailable",
)
def test_observer_reads_real_xvfb_process_and_window_tree(tmp_path: Path) -> None:
    published = tmp_path / "published" / "observer"
    builder.build_observer(published)
    binary = published.parent / "current" / published.name
    source = tmp_path / "wine_preloader.c"
    source.write_text(
        r"""
#include <X11/Xlib.h>
#include <X11/Xatom.h>
#include <X11/Xutil.h>
#include <stdio.h>
#include <stdlib.h>
#include <signal.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

static void reap_facts_child(int signal_number) {
    (void)signal_number;
    while (waitpid(-1, NULL, WNOHANG) > 0) {}
}

static int ignore_fixture_x_error(Display *display, XErrorEvent *event) {
    (void)display;
    (void)event;
    return 0;
}

int main(void) {
    (void)signal(SIGCHLD, reap_facts_child);
    const int facts_child = getenv("CROWLEY_FACTS_CHILD") != NULL;
    pid_t advertised_pid = getpid();
    if (facts_child) {
        prctl(PR_SET_NAME, "WowB.exe", 0, 0, 0);
        for (;;) sleep(1);
    }
    prctl(PR_SET_NAME, "fixture-host", 0, 0, 0);
    pid_t facts_pid = fork();
    if (facts_pid < 0) return 3;
    if (facts_pid == 0) {
        prctl(PR_SET_NAME, "WowB.exe", 0, 0, 0);
        for (;;) sleep(1);
    }
    advertised_pid = facts_pid;
    const char *facts_pid_path = getenv("CROWLEY_FACTS_PID_FILE");
    if (facts_pid_path) {
        FILE *facts_file = fopen(facts_pid_path, "w");
        if (!facts_file) return 4;
        (void)fprintf(facts_file, "%ld\n", (long)facts_pid);
        (void)fclose(facts_file);
        (void)chmod(facts_pid_path, 0600);
    }
    Display *display = XOpenDisplay(NULL);
    if (!display) return 2;
    XSetErrorHandler(ignore_fixture_x_error);
    Window root = DefaultRootWindow(display);
    Window outer = XCreateSimpleWindow(display, root, 0, 0, 1928, 1092, 0, 0, 0);
    Window child = XCreateSimpleWindow(display, outer, 0, 0, 1920, 1080, 0, 0, 0);
    Atom wm_name_atom = XInternAtom(display, "WM_NAME", False);
    Atom net_name_atom = XInternAtom(display, "_NET_WM_NAME", False);
    Atom utf8_atom = XInternAtom(display, "UTF8_STRING", False);
    Atom string_atom = XInternAtom(display, "STRING", False);
    Atom compound_atom = XInternAtom(display, "COMPOUND_TEXT", False);
    XStoreName(display, outer, "World of Warcraft");
    const char *initial_title = "World of Warcraft";
    XChangeProperty(display, outer, net_name_atom, utf8_atom, 8, PropModeReplace,
                    (unsigned char *)initial_title, strlen(initial_title));
    XClassHint hint = {"wowb.exe", "wowb.exe"};
    XSetClassHint(display, outer, &hint);
    Atom pid_atom = XInternAtom(display, "_NET_WM_PID", False);
    Atom wm_state_atom = XInternAtom(display, "WM_STATE", False);
    unsigned long pid = (unsigned long)advertised_pid;
    XChangeProperty(display, outer, pid_atom, XA_CARDINAL, 32, PropModeReplace,
                    (unsigned char *)&pid, 1);
    Atom active_atom = XInternAtom(display, "_NET_ACTIVE_WINDOW", False);
    unsigned long active = (unsigned long)child;
    XChangeProperty(display, root, active_atom, XA_WINDOW, 32, PropModeReplace,
                    (unsigned char *)&active, 1);
    unsigned long wm_state[2] = {1, 0};
    XChangeProperty(display, child, wm_state_atom, wm_state_atom, 32, PropModeReplace,
                    (unsigned char *)wm_state, 2);
    XMapWindow(display, child);
    XMapWindow(display, outer);
    XSetInputFocus(display, child, RevertToParent, CurrentTime);
    XFlush(display);
    const char *mutation_path = getenv("CROWLEY_MUTATION_FILE");
    Window extra = 0;
    Window invalid_outer = 0;
    Window duplicate_outer = 0;
    Window descendant = 0;
    Window unrelated = 0;
    for (;;) {
        char mutation[64] = "normal";
        if (mutation_path) {
            FILE *file = fopen(mutation_path, "r");
            if (file) { (void)fgets(mutation, sizeof(mutation), file); fclose(file); }
            mutation[strcspn(mutation, "\r\n")] = '\0';
        }
        const char *current_title = strcmp(mutation, "title") == 0 ? "Wrong title" : "World of Warcraft";
        if (strcmp(mutation, "wm-utf8") == 0) {
            XChangeProperty(display, outer, wm_name_atom, utf8_atom, 8, PropModeReplace,
                            (unsigned char *)current_title, strlen(current_title));
        } else if (strcmp(mutation, "wm-compound") == 0) {
            XChangeProperty(display, outer, wm_name_atom, compound_atom, 8, PropModeReplace,
                            (unsigned char *)current_title, strlen(current_title));
        } else {
            XStoreName(display, outer, current_title);
        }
        if (strcmp(mutation, "fallback") == 0) {
            XDeleteProperty(display, outer, net_name_atom);
        } else {
            Atom net_name_type = utf8_atom;
            if (strcmp(mutation, "net-string") == 0) net_name_type = string_atom;
            if (strcmp(mutation, "net-compound") == 0) net_name_type = compound_atom;
            XChangeProperty(display, outer, net_name_atom, net_name_type, 8, PropModeReplace,
                            (unsigned char *)current_title, strlen(current_title));
        }
        XClassHint current_hint = {"wowb.exe", "wowb.exe"};
        XClassHint wrong_hint = {"wrong.exe", "wrong.exe"};
        XClassHint wrong_name_hint = {"wrong.exe", "wowb.exe"};
        XClassHint wrong_class_hint = {"wowb.exe", "wrong.exe"};
        XClassHint *class_hint = &current_hint;
        if (strcmp(mutation, "class") == 0) class_hint = &wrong_hint;
        if (strcmp(mutation, "class-name") == 0) class_hint = &wrong_name_hint;
        if (strcmp(mutation, "class-class") == 0) class_hint = &wrong_class_hint;
        XSetClassHint(display, outer, class_hint);
        const char *replacement_pid_path = getenv("CROWLEY_REPLACEMENT_PID_FILE");
        if (replacement_pid_path) {
            FILE *replacement_file = fopen(replacement_pid_path, "r");
            if (replacement_file) {
                unsigned long replacement_pid = 0;
                if (fscanf(replacement_file, "%lu", &replacement_pid) == 1 && replacement_pid != 0) {
                    advertised_pid = (pid_t)replacement_pid;
                    (void)unlink(replacement_pid_path);
                }
                (void)fclose(replacement_file);
            }
        }
        unsigned long current_pid = (unsigned long)advertised_pid;
        unsigned long wrong_pid = current_pid + 1;
        XChangeProperty(display, outer, pid_atom, XA_CARDINAL, 32, PropModeReplace,
                        (unsigned char *)(strcmp(mutation, "pid") == 0 ? &wrong_pid : &current_pid), 1);
        if (strcmp(mutation, "geometry") == 0) {
            XResizeWindow(display, outer, 1, 1);
        } else {
            XResizeWindow(display, outer, 1928, 1092);
        }
        if (strcmp(mutation, "focus") == 0) {
            XSetInputFocus(display, root, RevertToParent, CurrentTime);
        } else if (strcmp(mutation, "none-focus") == 0) {
            XSetInputFocus(display, None, RevertToNone, CurrentTime);
        } else {
            XSetInputFocus(display, child, RevertToParent, CurrentTime);
        }
        unsigned long current_active = (unsigned long)child;
        unsigned long wrong_active = (unsigned long)root;
        if (strcmp(mutation, "noactive") == 0) {
            XDeleteProperty(display, root, active_atom);
        } else {
            XChangeProperty(display, root, active_atom, XA_WINDOW, 32, PropModeReplace,
                            (unsigned char *)(strcmp(mutation, "active") == 0 ? &wrong_active : &current_active), 1);
        }
        if (strcmp(mutation, "extra") == 0 && extra == 0) {
            extra = XCreateSimpleWindow(display, outer, 0, 0, 10, 10, 0, 0, 0);
            XMapWindow(display, extra);
        } else if (strcmp(mutation, "extra") != 0 && extra != 0) {
            XDestroyWindow(display, extra);
            extra = 0;
        }
        if (strcmp(mutation, "invalid-candidate") == 0 && invalid_outer == 0) {
            invalid_outer = XCreateSimpleWindow(display, root, 30, 30, 1, 1, 0, 0, 0);
            Window invalid_child = XCreateSimpleWindow(display, invalid_outer, 0, 0, 1, 1, 0, 0, 0);
            XStoreName(display, invalid_outer, "World of Warcraft");
            XChangeProperty(display, invalid_outer, net_name_atom, utf8_atom, 8, PropModeReplace,
                            (unsigned char *)initial_title, strlen(initial_title));
            XSetClassHint(display, invalid_outer, &hint);
            XChangeProperty(display, invalid_outer, pid_atom, XA_CARDINAL, 32, PropModeReplace,
                            (unsigned char *)&current_pid, 1);
            XMapWindow(display, invalid_child);
            XMapWindow(display, invalid_outer);
        } else if (strcmp(mutation, "invalid-candidate") != 0 && invalid_outer != 0) {
            XDestroyWindow(display, invalid_outer);
            invalid_outer = 0;
        }
        if (strcmp(mutation, "duplicate-window") == 0 && duplicate_outer == 0) {
            duplicate_outer = XCreateSimpleWindow(display, root, 0, 0, 1928, 1092, 0, 0, 0);
            Window duplicate_child = XCreateSimpleWindow(display, duplicate_outer, 0, 0, 1920, 1080, 0, 0, 0);
            XStoreName(display, duplicate_outer, "World of Warcraft");
            XChangeProperty(display, duplicate_outer, net_name_atom, utf8_atom, 8, PropModeReplace,
                            (unsigned char *)initial_title, strlen(initial_title));
            XSetClassHint(display, duplicate_outer, &hint);
            XChangeProperty(display, duplicate_outer, pid_atom, XA_CARDINAL, 32, PropModeReplace,
                            (unsigned char *)&current_pid, 1);
            XMapWindow(display, duplicate_child);
            XMapWindow(display, duplicate_outer);
            const char *duplicate_path = getenv("CROWLEY_DUPLICATE_WINDOW_FILE");
            if (duplicate_path) {
                FILE *duplicate_file = fopen(duplicate_path, "w");
                if (duplicate_file) {
                    (void)fprintf(duplicate_file, "0x%lx\n", (unsigned long)duplicate_outer);
                    (void)fclose(duplicate_file);
                    (void)chmod(duplicate_path, 0600);
                }
            }
        } else if (strcmp(mutation, "duplicate-window") != 0 && duplicate_outer != 0) {
            XDestroyWindow(display, duplicate_outer);
            duplicate_outer = 0;
            const char *duplicate_path = getenv("CROWLEY_DUPLICATE_WINDOW_FILE");
            if (duplicate_path) (void)unlink(duplicate_path);
        }
        if (strcmp(mutation, "descendant-focus") == 0 || strcmp(mutation, "outer-wm-state") == 0) {
            if (descendant == 0) {
                descendant = XCreateSimpleWindow(display, child, 0, 0, 100, 100, 0, 0, 0);
                XMapWindow(display, descendant);
            }
        } else if (descendant != 0) {
            XDestroyWindow(display, descendant);
            descendant = 0;
        }
        if (strcmp(mutation, "unrelated-focus") == 0 && unrelated == 0) {
            unrelated = XCreateSimpleWindow(display, root, 50, 50, 10, 10, 0, 0, 0);
            XMapWindow(display, unrelated);
        } else if (strcmp(mutation, "unrelated-focus") != 0 && unrelated != 0) {
            XDestroyWindow(display, unrelated);
            unrelated = 0;
        }
        if (strcmp(mutation, "outer-wm-state") == 0) {
            XDeleteProperty(display, child, wm_state_atom);
            XChangeProperty(display, outer, wm_state_atom, wm_state_atom, 32, PropModeReplace,
                            (unsigned char *)wm_state, 2);
        } else if (strcmp(mutation, "no-wm-state") == 0) {
            XDeleteProperty(display, child, wm_state_atom);
            XDeleteProperty(display, outer, wm_state_atom);
        } else {
            XChangeProperty(display, child, wm_state_atom, wm_state_atom, 32, PropModeReplace,
                            (unsigned char *)wm_state, 2);
            XDeleteProperty(display, outer, wm_state_atom);
        }
        if (strcmp(mutation, "reparent") == 0) {
            XReparentWindow(display, child, root, 0, 0);
        } else {
            XReparentWindow(display, child, outer, 0, 0);
        }
        if (strcmp(mutation, "vanish") == 0) {
            Window transient = XCreateSimpleWindow(display, root, 0, 0, 3, 3, 0, 0, 0);
            XMapWindow(display, transient);
            XFlush(display);
            usleep(50000);
            XDestroyWindow(display, transient);
        }
        if (strcmp(mutation, "focus") == 0) {
            XSetInputFocus(display, root, RevertToParent, CurrentTime);
        } else if (strcmp(mutation, "none-focus") == 0) {
            XSetInputFocus(display, None, RevertToNone, CurrentTime);
        } else if (strcmp(mutation, "descendant-focus") == 0 || strcmp(mutation, "outer-wm-state") == 0) {
            XSetInputFocus(display, descendant, RevertToParent, CurrentTime);
        } else if (strcmp(mutation, "unrelated-focus") == 0) {
            XSetInputFocus(display, unrelated, RevertToParent, CurrentTime);
        } else {
            XSetInputFocus(display, child, RevertToParent, CurrentTime);
        }
        XFlush(display);
        if (strcmp(mutation, "vanish") == 0) {
            usleep(100000);
        } else {
            sleep(1);
        }
    }
}
""",
        encoding="utf-8",
    )
    fixture = tmp_path / "wine-preloader"
    subprocess.run(["cc", str(source), "-lX11", "-o", str(fixture)], check=True)
    fixture.chmod(0o700)
    xdotool_source = tmp_path / "fixture_xdotool.c"
    xdotool_source.write_text(
        r"""
#include <X11/Xlib.h>
#include <X11/Xatom.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static Window parse_window(const char *value) {
    return (Window)strtoul(value, NULL, 0);
}

static Atom net_name_atom = 0;
static Atom wm_name_atom = 0;

static int name_matches(Display *display, Window window, const char *needle) {
    const Atom properties[] = {net_name_atom, wm_name_atom};
    const size_t needle_length = strlen(needle);
    for (size_t index = 0; index < sizeof(properties) / sizeof(properties[0]); ++index) {
        Atom actual = 0;
        int format = 0;
        unsigned long count = 0, after = 0;
        unsigned char *data = NULL;
        if (XGetWindowProperty(display, window, properties[index], 0, 4096, False, AnyPropertyType,
                               &actual, &format, &count, &after, &data) != Success) {
            if (data != NULL) XFree(data);
            continue;
        }
        if (data != NULL && format == 8 && after == 0) {
            for (unsigned long offset = 0; offset + needle_length <= count; ++offset) {
                if (memcmp(data + offset, needle, needle_length) == 0) {
                    XFree(data);
                    return 1;
                }
            }
        }
        if (data != NULL) XFree(data);
    }
    return 0;
}

static void search(Display *display, Window window, const char *needle) {
    Window root = 0, parent = 0, *children = NULL;
    unsigned int count = 0;
    if (!XQueryTree(display, window, &root, &parent, &children, &count)) return;
    if (name_matches(display, window, needle)) {
        printf("0x%lx\n", (unsigned long)window);
    }
    for (unsigned int index = 0; index < count; ++index) search(display, children[index], needle);
    if (children != NULL) XFree(children);
}

int main(int argc, char **argv) {
    Display *display = XOpenDisplay(NULL);
    if (!display || argc < 2) return 2;
        net_name_atom = XInternAtom(display, "_NET_WM_NAME", False);
        wm_name_atom = XInternAtom(display, "WM_NAME", False);
    if (strcmp(argv[1], "search") == 0 && argc == 4 && strcmp(argv[2], "--name") == 0) {
        search(display, DefaultRootWindow(display), argv[3]);
    } else if (strcmp(argv[1], "getwindowgeometry") == 0 && argc == 4 && strcmp(argv[2], "--shell") == 0) {
        Window window = parse_window(argv[3]), root = 0;
        int x = 0, y = 0;
        unsigned int width = 0, height = 0, border = 0, depth = 0;
        if (!XGetGeometry(display, window, &root, &x, &y, &width, &height, &border, &depth)) return 3;
        printf("WINDOW=0x%lx\nX=%d\nY=%d\nWIDTH=%u\nHEIGHT=%u\nSCREEN=0\n",
               (unsigned long)window, x, y, width, height);
    } else if (strcmp(argv[1], "getwindowfocus") == 0 && argc == 2) {
        Window focus = None;
        int revert = RevertToNone;
        XGetInputFocus(display, &focus, &revert);
        printf("0x%lx\n", (unsigned long)focus);
    } else {
        return 4;
    }
    XCloseDisplay(display);
    return 0;
}
""",
        encoding="utf-8",
    )
    tool_bin = tmp_path / "reference-bin"
    tool_bin.mkdir(mode=0o700)
    xdotool = tool_bin / "xdotool"
    subprocess.run(["cc", str(xdotool_source), "-lX11", "-o", str(xdotool)], check=True)
    xdotool.chmod(0o700)
    query_proxy_source = tmp_path / "fixture_x_query_proxy.c"
    query_proxy_source.write_text(
        r"""
#define _GNU_SOURCE
#include <X11/Xlib.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

typedef ssize_t (*writev_fn)(int, const struct iovec *, int);
static writev_fn real_writev = NULL;
static int destroyed = 0;
static __thread int handling = 0;

static long long monotonic_ns(void) {
    struct timespec now;
    (void)clock_gettime(CLOCK_MONOTONIC, &now);
    return (long long)now.tv_sec * 1000000000LL + now.tv_nsec;
}

static void record_event(const char *name) {
    const char *path = getenv("CROWLEY_QUERY_BARRIER_MARKER");
    if (path == NULL) return;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd < 0) return;
    (void)dprintf(fd, "%s=%lld\n", name, monotonic_ns());
    (void)close(fd);
    (void)chmod(path, 0600);
}

int has_query_for_target(const unsigned char *bytes, size_t length, Window target) {
    size_t offset = 0;
    while (offset + 4 <= length) {
        unsigned long request_length = (unsigned long)bytes[offset + 2]
            | ((unsigned long)bytes[offset + 3] << 8);
        if (request_length == 0 || request_length * 4 > length - offset) return 0;
        if (bytes[offset] == 15 && request_length == 2) {  /* X_QueryTree */
            uint32_t requested = (uint32_t)bytes[offset + 4]
                | ((uint32_t)bytes[offset + 5] << 8)
                | ((uint32_t)bytes[offset + 6] << 16)
                | ((uint32_t)bytes[offset + 7] << 24);
            if ((uint32_t)target == requested) return 1;
        }
        offset += request_length * 4;
    }
    return 0;
}

ssize_t writev(int fd, const struct iovec *iov, int iovcnt) {
    if (real_writev == NULL) real_writev = (writev_fn)dlsym(RTLD_NEXT, "writev");
    if (real_writev == NULL) return -1;
    ssize_t written = real_writev(fd, iov, iovcnt);
    if (handling || destroyed || written <= 0) return written;
    const char *target_text = getenv("CROWLEY_QUERY_BARRIER_WINDOW");
    if (target_text != NULL) {
        Window target = (Window)strtoul(target_text, NULL, 0);
        unsigned char bytes[4096];
        size_t used = 0;
        for (int index = 0; index < iovcnt && used < sizeof(bytes); ++index) {
            size_t amount = iov[index].iov_len;
            if (amount > sizeof(bytes) - used) amount = sizeof(bytes) - used;
            (void)memcpy(bytes + used, iov[index].iov_base, amount);
            used += amount;
        }
        if ((size_t)written < used) used = (size_t)written;
        if (target != 0 && has_query_for_target(bytes, used, target)) {
            destroyed = 1;
            record_event("query_request_written");
            handling = 1;
            Display *display = XOpenDisplay(NULL);
            if (display != NULL) {
                (void)XDestroyWindow(display, target);
                (void)XSync(display, False);
                XCloseDisplay(display);
                record_event("candidate_destroyed");
            }
            handling = 0;
        }
    }
    return written;
}
""",
        encoding="utf-8",
    )
    query_proxy = tmp_path / "fixture-x-query-proxy.so"
    subprocess.run(
        ["cc", "-shared", "-fPIC", str(query_proxy_source), "-lX11", "-ldl", "-o", str(query_proxy)],
        check=True,
    )
    query_proxy.chmod(0o700)
    query_wrapper_source = tmp_path / "fixture_x_query_wrapper.c"
    query_wrapper_source.write_text(
        r"""
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char **argv) {
    const char *observer = getenv("CROWLEY_REAL_OBSERVER");
    const char *proxy = getenv("CROWLEY_QUERY_PROXY");
    if (observer == NULL || proxy == NULL) return 97;
    if (setenv("LD_PRELOAD", proxy, 1) != 0) return 98;
    (void)execv(observer, argv);
    return 99;
}
""",
        encoding="utf-8",
    )
    query_wrapper = tmp_path / "fixture-x-query-wrapper"
    subprocess.run(["cc", str(query_wrapper_source), "-o", str(query_wrapper)], check=True)
    query_wrapper.chmod(0o700)
    pinned_xdotool_root = builder.build_xdotool(builder.VENDORED_SOURCE_ARCHIVE, tmp_path / "pinned-xdotool")
    pinned_xdotool = pinned_xdotool_root / "current" / pins.XDOTOOL_ARTIFACT_NAME
    gate_source = tmp_path / "fixture_x_gate.c"
    gate_source.write_text(
        r"""
#include <X11/Xlib.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 4) return 2;
    if (setenv("DISPLAY", argv[1], 1) != 0) return 3;
    Display *display = XOpenDisplay(NULL);
    if (!display) return 4;
    XGrabServer(display);
    XSync(display, False);
    int marker = open(argv[2], O_WRONLY | O_CREAT | O_TRUNC, 0600);
    if (marker < 0) return 5;
    struct timespec grabbed;
    (void)clock_gettime(CLOCK_MONOTONIC, &grabbed);
    (void)dprintf(marker, "serverGrabbedMonotonicNs=%lld\n",
                  (long long)grabbed.tv_sec * 1000000000LL + grabbed.tv_nsec);
    (void)close(marker);
    (void)chmod(argv[2], 0600);
    while (access(argv[3], F_OK) != 0) usleep(1000);
    XUngrabServer(display);
    XFlush(display);
    XCloseDisplay(display);
    return 0;
}
""",
        encoding="utf-8",
    )
    gate_fixture = tmp_path / "fixture-x-gate"
    subprocess.run(["cc", str(gate_source), "-lX11", "-o", str(gate_fixture)], check=True)
    gate_fixture.chmod(0o700)
    xvfb, display, x11, xdisplay = start_private_xvfb(
        lambda display_fd: (
            "Xvfb",
            "-displayfd",
            str(display_fd),
            "-screen",
            "0",
            "1928x1092x24",
            "-ac",
        ),
        popen_kwargs={
            "close_fds": True,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
        },
    )
    x11.XCloseDisplay(xdisplay)
    process = None
    duplicate = None
    gate = None
    target_pids: list[int] = []
    try:
        prefix_target = tmp_path / "wine-prefix-target"
        prefix_target.mkdir()
        prefix = tmp_path / "wine-prefix"
        prefix.symlink_to(prefix_target, target_is_directory=True)
        mutation_file = tmp_path / "observer-mutation"
        mutation_file.write_text("normal\n", encoding="ascii")
        mutation_file.chmod(0o600)
        facts_pid_file = tmp_path / "observer-facts-pid"
        replacement_pid_file = tmp_path / "observer-replacement-pid"
        environment = {
            "DISPLAY": display,
            "WINEPREFIX": str(prefix) + "/",
            "CROWLEY_MUTATION_FILE": str(mutation_file),
            "CROWLEY_FACTS_PID_FILE": str(facts_pid_file),
            "CROWLEY_REPLACEMENT_PID_FILE": str(replacement_pid_file),
            "CROWLEY_DUPLICATE_WINDOW_FILE": str(tmp_path / "observer-duplicate-window"),
        }
        process = subprocess.Popen([str(fixture), "WowB.exe"], env=environment)
        pid_deadline = time.monotonic() + 5
        target_pid = None
        while time.monotonic() < pid_deadline:
            try:
                target_pid = int(facts_pid_file.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                time.sleep(0.01)
                continue
            if target_pid > 0:
                break
        assert target_pid is not None and target_pid > 0
        target_pids.append(target_pid)
        request = _request(
            display=display,
            winePrefix=str(prefix) + "/",
            binaryDigest=hashlib.sha256(binary.read_bytes()).hexdigest(),
        )
        deadline = time.monotonic() + 5
        result = None
        while time.monotonic() < deadline:
            result = subprocess.run(
                [str(binary)], input=(request + "\n").encode(), capture_output=True, check=False
            )
            if result.returncode == 0:
                break
            time.sleep(0.05)
        assert result is not None and result.returncode == 0, result.stderr.decode()
        facts = json.loads(result.stdout)
        assert set(facts) == {
            "protocolVersion",
            "matchingProcessCount",
            "candidateWindowCount",
            "pid",
            "start",
            "exe",
            "statusName",
            "loader",
            "winePrefix",
            "xid",
            "title",
            "class",
            "outer",
            "child",
            "outerRoot",
            "outerParent",
            "outerGeometry",
            "childGeometry",
            "outerChildren",
            "focus",
            "active",
            "display",
            "nonce",
            "binaryDigest",
            "timingsNs",
        }
        assert facts["protocolVersion"] == 1
        assert set(facts["timingsNs"]) == {"proc", "x11", "total"}
        assert all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0
            for value in facts["timingsNs"].values()
        )
        assert facts["timingsNs"]["total"] >= facts["timingsNs"]["x11"]
        assert facts["matchingProcessCount"] == 1
        assert facts["candidateWindowCount"] == 1
        assert facts["statusName"] == "WowB.exe"
        assert facts["class"] == "wowb.exe,wowb.exe"
        assert facts["loader"] == "wine-preloader"
        assert facts["winePrefix"] == str(prefix_target)
        assert facts["outerGeometry"] == [0, 0, 1928, 1092]
        assert facts["childGeometry"] == [0, 0, 1920, 1080]
        assert facts["outerChildren"] == [facts["child"]]
        assert facts["outerParent"] == facts["outerRoot"]
        assert facts["focus"] == facts["child"]
        assert facts["active"] == facts["child"]
        xauthority = tmp_path / "Xauthority"
        xauthority.touch(mode=0o600)
        binary.chmod(0o700)
        profile = DriverProfile(
            display=display,
            xauthority=str(xauthority),
            wine_prefix=str(prefix),
            identity_observer_executable=binary,
            identity_observer_executable_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        )
        backend = X11Backend.production(
            profile,
            input_tool=fixture_input_tool(
                tmp_path / "production-input-tool",
                executable_source=str(pinned_xdotool),
            ),
        )

        def assert_reference_parity(*, require_foreground: bool = True):
            production_identity = backend.resolve_identity(require_foreground=require_foreground)
            reference = X11Backend(
                profile,
                input_tool=backend.input_tool,
            )
            try:
                reference_identity = reference.resolve_identity(require_foreground=require_foreground)
            finally:
                reference.close()
            assert production_identity == reference_identity
            assert production_identity.fingerprint == reference_identity.fingerprint
            assert production_identity.wm_class.split(",") == [profile.window_class, profile.window_class]
            return production_identity

        identity = assert_reference_parity()
        assert identity.pid == target_pid
        assert identity.executable == "WowB.exe"
        assert backend.resolver_kind == "pinned-rust-v1"

        # The proxy destroys one already title/class/PID-matching duplicate
        # immediately after its real XQueryTree request is written.  The other
        # matching window remains valid, so suppressing the resulting geometry
        # error would incorrectly turn an uncertain observation into acceptance.
        def run_query_uncertainty(observer: Path, observer_digest: str, marker: Path):
            environment_with_proxy = os.environ.copy()
            environment_with_proxy.update(
                {
                    "DISPLAY": display,
                    "XAUTHORITY": str(xauthority),
                    "CROWLEY_REAL_OBSERVER": str(observer),
                    "CROWLEY_QUERY_PROXY": str(query_proxy),
                    "CROWLEY_QUERY_BARRIER_WINDOW": duplicate_window,
                    "CROWLEY_QUERY_BARRIER_MARKER": str(marker),
                }
            )
            result = subprocess.run(
                [str(query_wrapper)],
                input=(
                    _request(display=display, winePrefix=str(prefix) + "/", binaryDigest=observer_digest)
                    + "\n"
                ).encode(),
                env=environment_with_proxy,
                capture_output=True,
                check=False,
            )
            raw_stdout = result.stdout.decode("utf-8", "replace")
            raw_stderr = result.stderr.decode("utf-8", "replace")
            response = json.loads(raw_stdout)
            receipt = tmp_path / f"{marker.name}.json"
            receipt.write_text(
                json.dumps(
                    {
                        "observer": str(observer),
                        "observerSha256": observer_digest,
                        "returncode": result.returncode,
                        "stdout": raw_stdout,
                        "stderr": raw_stderr,
                        "marker": marker.read_text(encoding="ascii") if marker.is_file() else "",
                        "response": response,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            receipt.chmod(0o600)
            return result, response, receipt

        mutation_file.write_text("duplicate-window\n", encoding="ascii")
        duplicate_path = Path(environment["CROWLEY_DUPLICATE_WINDOW_FILE"])
        duplicate_deadline = time.monotonic() + 5
        while time.monotonic() < duplicate_deadline and not duplicate_path.is_file():
            time.sleep(0.01)
        assert duplicate_path.is_file()
        duplicate_window = duplicate_path.read_text(encoding="ascii").strip()
        mutation_file.write_text("normal\n", encoding="ascii")
        time.sleep(1.15)
        mutation_file.write_text("duplicate-window\n", encoding="ascii")
        duplicate_deadline = time.monotonic() + 5
        while time.monotonic() < duplicate_deadline and not duplicate_path.is_file():
            time.sleep(0.01)
        assert duplicate_path.is_file()
        duplicate_window = duplicate_path.read_text(encoding="ascii").strip()
        uncertainty_marker = tmp_path / "observer-query-uncertainty"
        uncertain, uncertain_response, uncertain_receipt = run_query_uncertainty(
            binary,
            hashlib.sha256(binary.read_bytes()).hexdigest(),
            uncertainty_marker,
        )
        assert uncertain.returncode != 0
        assert uncertain_response == {"protocolVersion": 1, "error": "transient X11 window error"}
        uncertainty_events = {
            line.split("=", 1)[0]: int(line.split("=", 1)[1])
            for line in uncertainty_marker.read_text(encoding="ascii").splitlines()
            if "=" in line
        }
        assert set(uncertainty_events) == {"query_request_written", "candidate_destroyed"}
        assert uncertainty_events["query_request_written"] < uncertainty_events["candidate_destroyed"]
        assert uncertain_receipt.is_file()
        mutation_file.write_text("normal\n", encoding="ascii")
        time.sleep(1.15)
        restored_after_uncertainty = subprocess.run(
            [str(binary)], input=(request + "\n").encode(), capture_output=True, check=False
        )
        assert restored_after_uncertainty.returncode == 0, restored_after_uncertainty.stderr.decode()
        assert json.loads(restored_after_uncertainty.stdout)["pid"] == target_pid

        # The gate owns the X server grab before the observer starts.  The
        # observer's socket can appear only after its initial /proc snapshot,
        # while its XOpenDisplay handshake remains blocked by the gate.  Kill
        # and replace the advertised facts child, then release the server so
        # the final process read must exercise the process binding guard while
        # the window publisher and its X facts remain alive.
        gate_marker = tmp_path / "observer-x-gate-grabbed"
        gate_release = tmp_path / "observer-x-gate-release"
        gate = subprocess.Popen([str(gate_fixture), display, str(gate_marker), str(gate_release)])
        gate_deadline = time.monotonic() + 5
        while time.monotonic() < gate_deadline and not gate_marker.exists():
            time.sleep(0.01)
        assert gate_marker.is_file()
        gate_marker_text = gate_marker.read_text(encoding="ascii").strip()
        gate_grabbed_ns = int(gate_marker_text.split("=", 1)[1])
        barrier = tmp_path / "observer-lifetime-barrier.json"
        replacement_box: dict[str, subprocess.Popen[bytes]] = {}
        observer_process = subprocess.Popen(
            [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        assert observer_process.stdin is not None
        observer_process.stdin.write((request + "\n").encode())
        observer_process.stdin.close()
        observer_process.stdin = None
        socket_deadline = time.monotonic() + 5
        while time.monotonic() < socket_deadline:
            if observer_process.poll() is not None:
                break
            if _process_has_x_socket(observer_process.pid):
                break
            time.sleep(0.001)
        assert observer_process.poll() is None
        assert _process_has_x_socket(observer_process.pid)
        observer_socket_ns = time.monotonic_ns()
        old_target_pid = target_pid
        os.kill(old_target_pid, 9)
        target_deadline = time.monotonic() + 5
        while time.monotonic() < target_deadline and Path(f"/proc/{old_target_pid}").exists():
            time.sleep(0.001)
        assert not Path(f"/proc/{old_target_pid}").exists()
        replaced_ns = time.monotonic_ns()
        replacement = subprocess.Popen(
            [str(fixture), "WowB.exe"],
            env={**environment, "CROWLEY_FACTS_CHILD": "1"},
        )
        replacement_box["process"] = replacement
        target_pids = [old_target_pid, replacement.pid]
        replacement_pid_file.write_text(f"{replacement.pid}\n", encoding="ascii")
        replacement_pid_file.chmod(0o600)
        replacement_started_ns = time.monotonic_ns()
        gate_release.touch(mode=0o600)
        released_ns = time.monotonic_ns()
        assert gate.wait(timeout=5) == 0
        observer_stdout, observer_stderr = observer_process.communicate(timeout=10)
        assert observer_process.returncode != 0, observer_stdout.decode()
        assert observer_process.returncode != -6, observer_stderr.decode()
        mixed_lifetime = json.loads(observer_stdout)
        assert mixed_lifetime == {
            "protocolVersion": 1,
            "error": "process binding changed during observation",
        }
        replacement = replacement_box.get("process")
        assert replacement is not None and replacement.poll() is None
        assert gate_grabbed_ns < observer_socket_ns < replaced_ns < replacement_started_ns < released_ns
        barrier.write_text(
            json.dumps(
                {
                    "gateGrabbedMonotonicNs": gate_grabbed_ns,
                    "observerSocketMonotonicNs": observer_socket_ns,
                    "initialTargetPid": old_target_pid,
                    "replacementTargetPid": replacement.pid,
                    "targetReplacedMonotonicNs": replaced_ns,
                    "replacementStartedMonotonicNs": replacement_started_ns,
                    "gateReleasedMonotonicNs": released_ns,
                    "firstResponse": mixed_lifetime,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        barrier.chmod(0o600)
        target_pid = replacement.pid
        restored_result = None
        restored_deadline = time.monotonic() + 5
        while time.monotonic() < restored_deadline:
            restored_result = subprocess.run(
                [str(binary)], input=(request + "\n").encode(), capture_output=True, check=False
            )
            if restored_result.returncode == 0:
                break
            time.sleep(0.05)
        assert restored_result is not None and restored_result.returncode == 0, (
            restored_result.stderr.decode() if restored_result else "missing restored result"
        )
        restored_facts = json.loads(restored_result.stdout)
        assert restored_facts["pid"] == target_pid
        restored_identity = assert_reference_parity()
        assert restored_identity.pid == target_pid
        evidence_root = tmp_path / "executor-evidence" / "native-identity-observer-ownerb-20261020"
        evidence_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        evidence_root.chmod(0o700)
        evidence_path = evidence_root / f"b4-lifetime-barrier-{os.getpid()}-{time.time_ns()}.json"
        evidence_path.write_text(
            json.dumps(
                {
                    "barrier": json.loads(barrier.read_text(encoding="utf-8")),
                    "first": {"returncode": observer_process.returncode, "response": mixed_lifetime},
                    "replacementPid": replacement.pid,
                    "restored": {"returncode": restored_result.returncode, "pid": restored_facts["pid"]},
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        evidence_path.chmod(0o600)
        # Keep one representative of each expensive real-display boundary:
        # root focus, descendant focus/ancestor, process PID, geometry, candidate filtering, and
        # duplicate-window rejection.  The protocol matrix above and the
        # static request checks below cover equivalent title/class/property
        # spellings without replaying every one-second compositor mutation.
        for mutation in (
            "focus",
            "descendant-focus",
            "pid",
            "geometry",
            "invalid-candidate",
            "duplicate-window",
        ):
            mutation_file.write_text(mutation + "\n", encoding="ascii")
            time.sleep(1.15)
            rejected = subprocess.run(
                [str(binary)],
                input=(request + "\n").encode(),
                capture_output=True,
                check=False,
            )
            if mutation == "vanish":
                assert rejected.returncode != -6, rejected.stderr.decode()
                if rejected.returncode != 0:
                    assert json.loads(rejected.stdout).keys() == {"protocolVersion", "error"}
            elif mutation == "none-focus":
                # The active-window property remains the game, but the real
                # input focus is None.  Both the production pinned observer
                # and the actual vendored xdotool reference must refuse this
                # state even when the foreground guard is disabled.
                for require_foreground in (True, False):
                    with pytest.raises(DriverError):
                        backend.resolve_identity(require_foreground=require_foreground)
                    reference = X11Backend(profile, input_tool=backend.input_tool)
                    try:
                        with pytest.raises(DriverError):
                            reference.resolve_identity(require_foreground=require_foreground)
                    finally:
                        reference.close()
                assert json.loads(rejected.stdout).keys() == {"protocolVersion", "error"}
            elif mutation in {
                "focus",
                "no-wm-state",
                "descendant-focus",
                "outer-wm-state",
                "unrelated-focus",
                "active",
                "noactive",
                "net-string",
                "net-compound",
                "wm-utf8",
                "wm-compound",
                "invalid-candidate",
                "fallback",
            }:
                assert rejected.returncode == 0, (mutation, rejected.stderr.decode())
                changed = json.loads(rejected.stdout)
                if mutation == "fallback":
                    assert changed["title"] == "World of Warcraft"
                elif mutation == "noactive":
                    assert changed["active"] == 0
                elif mutation == "no-wm-state":
                    assert changed["focus"] == changed["child"]
                elif mutation == "descendant-focus":
                    assert changed["focus"] == changed["child"]
                elif mutation == "outer-wm-state":
                    assert changed["focus"] == changed["outer"]
                elif mutation == "unrelated-focus":
                    assert changed["focus"] != changed["child"]
                elif mutation in {"net-string", "net-compound", "wm-utf8", "wm-compound"}:
                    assert changed["title"] == "World of Warcraft"
                elif mutation == "invalid-candidate":
                    assert changed["candidateWindowCount"] == 1
                    assert changed["outerParent"] == changed["outerRoot"]
                else:
                    assert (
                        changed["focus"] != changed["child"]
                        or changed["active"] != changed["child"]
                        or changed["outerChildren"] != [changed["child"]]
                    )
                if mutation in {
                    "focus",
                    "no-wm-state",
                    "descendant-focus",
                    "outer-wm-state",
                    "unrelated-focus",
                    "active",
                    "noactive",
                    "net-string",
                    "net-compound",
                    "wm-utf8",
                    "wm-compound",
                    "fallback",
                    "invalid-candidate",
                }:
                    try:
                        variant_identity = assert_reference_parity(require_foreground=False)
                    except Exception as exc:
                        raise AssertionError(f"reference parity failed for {mutation}: {exc}") from exc
                    assert variant_identity.pid == target_pid
            else:
                assert rejected.returncode != 0, mutation
                assert json.loads(rejected.stdout).keys() == {"protocolVersion", "error"}
            mutation_file.write_text("normal\n", encoding="ascii")
            time.sleep(1.15)
            restored = subprocess.run(
                [str(binary)],
                input=(request + "\n").encode(),
                capture_output=True,
                check=False,
            )
            assert restored.returncode == 0, (mutation, restored.stderr.decode())
        backend.close()
        for mutation in (
            {"title": "Wrong title"},
            {"class": "wrong-class"},
            {"outerWidth": 1},
            {"clientWidth": 1},
        ):
            rejected = subprocess.run(
                [str(binary)],
                input=(_request(display=display, winePrefix=str(prefix), **mutation) + "\n").encode(),
                capture_output=True,
                check=False,
            )
            assert rejected.returncode != 0, mutation
            assert json.loads(rejected.stdout).keys() == {"protocolVersion", "error"}
        duplicate = subprocess.Popen(
            [str(fixture), "WowB.exe"],
            env={**environment, "CROWLEY_FACTS_CHILD": "1"},
        )
        duplicate_deadline = time.monotonic() + 2
        while time.monotonic() < duplicate_deadline:
            if duplicate.poll() is None:
                try:
                    status = Path(f"/proc/{duplicate.pid}/status").read_text(encoding="utf-8")
                except OSError:
                    status = ""
                if "Name:\tWowB.exe" in status:
                    break
            time.sleep(0.02)
        else:
            pytest.fail("duplicate fixture process did not reach its verified process name")
        rejected = subprocess.run(
            [str(binary)],
            input=(request + "\n").encode(),
            capture_output=True,
            check=False,
        )
        assert rejected.returncode != 0, rejected.stdout.decode()
        assert b"one verified game process" in rejected.stdout
    finally:
        if gate is not None and gate.poll() is None:
            gate_release.touch(mode=0o600)
            gate.wait(timeout=5)
        if duplicate is not None:
            duplicate.terminate()
            duplicate.wait(timeout=5)
        for pid in target_pids:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                continue
        if process is not None:
            process.terminate()
            process.wait(timeout=5)
        xvfb.terminate()
        xvfb.wait(timeout=5)
