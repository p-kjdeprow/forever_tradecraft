"""Minimal EWMH window manager for private X11 test displays."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

_SOURCE = r"""
#include <X11/Xlib.h>
#include <X11/Xatom.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static volatile sig_atomic_t stopping = 0;

static void stop_manager(int signal_number) {
    (void)signal_number;
    stopping = 1;
}

static int ignore_x_error(Display *display, XErrorEvent *error) {
    (void)display;
    (void)error;
    return 0;
}

static void publish_normal_state(Display *display, Window window, Atom wm_state_atom) {
    unsigned long state[2] = {1, None};
    XChangeProperty(display, window, wm_state_atom, wm_state_atom, 32,
                    PropModeReplace, (unsigned char *)state, 2);
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    (void)signal(SIGINT, stop_manager);
    (void)signal(SIGTERM, stop_manager);
    Display *display = XOpenDisplay(NULL);
    if (display == NULL) return 3;
    XSetErrorHandler(ignore_x_error);
    Window root = DefaultRootWindow(display);
    Atom supported_atom = XInternAtom(display, "_NET_SUPPORTED", False);
    Atom active_atom = XInternAtom(display, "_NET_ACTIVE_WINDOW", False);
    Atom wm_state_atom = XInternAtom(display, "WM_STATE", False);
    Atom supported[] = {active_atom};
    XChangeProperty(display, root, supported_atom, XA_ATOM, 32, PropModeReplace,
                    (unsigned char *)supported, 1);
    Window active_window = None;
    XChangeProperty(display, root, active_atom, XA_WINDOW, 32, PropModeReplace,
                    (unsigned char *)&active_window, 1);
    XSelectInput(display, root, SubstructureRedirectMask | SubstructureNotifyMask);
    XSync(display, False);
    FILE *ready = fopen(argv[1], "w");
    if (ready == NULL) {
        XCloseDisplay(display);
        return 4;
    }
    (void)fputs("ready\n", ready);
    (void)fclose(ready);
    while (!stopping) {
        while (XPending(display) > 0) {
            XEvent event;
            XNextEvent(display, &event);
            if (event.type == MapRequest) {
                publish_normal_state(display, event.xmaprequest.window, wm_state_atom);
                XMapWindow(display, event.xmaprequest.window);
            } else if (event.type == ConfigureRequest) {
                XWindowChanges changes = {
                    event.xconfigurerequest.x,
                    event.xconfigurerequest.y,
                    event.xconfigurerequest.width,
                    event.xconfigurerequest.height,
                    event.xconfigurerequest.border_width,
                    event.xconfigurerequest.above,
                    event.xconfigurerequest.detail,
                };
                XConfigureWindow(
                    display,
                    event.xconfigurerequest.window,
                    event.xconfigurerequest.value_mask,
                    &changes
                );
            } else if (event.type == ClientMessage
                       && event.xclient.message_type == active_atom
                       && event.xclient.format == 32) {
                Window requested = event.xclient.window;
                if (requested == None || requested == root) {
                    requested = (Window)event.xclient.data.l[2];
                }
                if (requested != None && requested != root) {
                    publish_normal_state(display, requested, wm_state_atom);
                    XMapRaised(display, requested);
                    XSetInputFocus(display, requested, RevertToPointerRoot, CurrentTime);
                    active_window = requested;
                    XChangeProperty(display, root, active_atom, XA_WINDOW, 32,
                                    PropModeReplace, (unsigned char *)&active_window, 1);
                }
            }
        }
        XFlush(display);
        usleep(1000);
    }
    XCloseDisplay(display);
    return 0;
}
"""


def start_ewmh_window_manager(*, display: str, xauthority: str, workdir: Path) -> subprocess.Popen[bytes]:
    """Compile and start the minimal EWMH manager on one private display."""

    compiler = shutil.which("cc")
    if compiler is None:
        raise RuntimeError("C compiler is unavailable for the EWMH test fixture")
    workdir.mkdir(mode=0o700)
    source = workdir / "ewmh_window_manager.c"
    binary = workdir / "ewmh_window_manager"
    ready = workdir / "ready"
    source.write_text(_SOURCE, encoding="ascii")
    subprocess.run([compiler, str(source), "-lX11", "-o", str(binary)], check=True)
    binary.chmod(0o700)
    environment = os.environ.copy()
    environment.update({"DISPLAY": display, "XAUTHORITY": xauthority})
    process = subprocess.Popen(
        [str(binary), str(ready)],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if ready.is_file():
            return process
        if process.poll() is not None:
            detail = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            raise RuntimeError(f"EWMH test window manager exited before readiness: {detail.strip()}")
        time.sleep(0.01)
    stop_ewmh_window_manager(process)
    raise RuntimeError("EWMH test window manager did not become ready")


def stop_ewmh_window_manager(process: subprocess.Popen[bytes]) -> None:
    """Stop a manager returned by :func:`start_ewmh_window_manager`."""

    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2.0)
