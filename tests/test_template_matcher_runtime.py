"""Focused tests for the pinned matcher runtime adapter.

These tests require the retained private matcher release.  Set
``CROWLEY_TEMPLATE_MATCHER_ARTIFACT_ROOT`` to its release directory, or place
the fixture at the runtime's canonical private cache path.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from crowley.native import matcher as runtime
from tools import native_calibration_operator as operator

_ARTIFACT_ROOT_ENV = "CROWLEY_TEMPLATE_MATCHER_ARTIFACT_ROOT"


def _release_paths() -> tuple[Path, Path, Path]:
    configured = os.environ.get(_ARTIFACT_ROOT_ENV)
    root = Path(configured).expanduser() if configured else runtime.default_artifact_root()
    artifact = root / runtime.ARTIFACT_NAME
    manifest = root / runtime.MANIFEST_NAME
    if not artifact.is_file() or not manifest.is_file():
        pytest.fail(
            "retained matcher release fixture is unavailable; set "
            f"{_ARTIFACT_ROOT_ENV} to the private release directory"
        )
    return root, artifact, manifest


def _assert_release_fixture() -> tuple[Path, Path, Path]:
    root, artifact, manifest = _release_paths()
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == runtime.EXPECTED_ARTIFACT_SHA256
    return root, artifact, manifest


def _pattern(width: int, height: int) -> bytes:
    return bytes(
        (x * 31 + y * 17 + channel * 7) % 256
        for y in range(height)
        for x in range(width)
        for channel in range(3)
    )


def _crop(frame: bytes, frame_size: tuple[int, int], box: tuple[int, int, int, int]) -> bytes:
    frame_width, _frame_height = frame_size
    left, top, width, height = box
    row_bytes = frame_width * 3
    result = bytearray()
    for row in range(top, top + height):
        start = row * row_bytes + left * 3
        result.extend(frame[start : start + width * 3])
    return bytes(result)


def _reference(
    frame: bytes,
    frame_size: tuple[int, int],
    template: bytes,
    template_size: tuple[int, int],
    region: tuple[int, int, int, int],
    operation: int,
) -> runtime.MatcherResult:
    template_width, template_height = template_size
    left, top, region_width, region_height = region
    count = len(template)

    def sad_at(x: int, y: int) -> int:
        candidate = _crop(frame, frame_size, (x, y, template_width, template_height))
        return sum(abs(a - b) for a, b in zip(candidate, template, strict=True))

    if operation == 2:
        sad = sad_at(left, top)
        status = "matched" if sad <= 5 * count else "rejected"
        return runtime.MatcherResult(status, sad, count, (left, top, template_width, template_height))

    candidates = [
        (sad_at(x, y), x, y)
        for y in range(top, top + region_height - template_height + 1)
        for x in range(left, left + region_width - template_width + 1)
    ]
    sad, x, y = min(candidates)
    acceptable = [candidate for candidate in candidates if candidate[0] <= 5 * count]
    ambiguous = any(
        (other_x, other_y) != (x, y)
        and not (
            x < other_x + template_width
            and other_x < x + template_width
            and y < other_y + template_height
            and other_y < y + template_height
        )
        for _other_sad, other_x, other_y in acceptable
    )
    status = "ambiguous" if ambiguous else ("matched" if sad <= 5 * count else "rejected")
    return runtime.MatcherResult(status, sad, count, (x, y, template_width, template_height))


def _request_args() -> tuple[bytes, tuple[int, int], bytes, tuple[int, int], tuple[int, int, int, int]]:
    frame_size = (7, 5)
    frame = _pattern(*frame_size)
    box = (3, 2, 2, 2)
    template_size = (box[2], box[3])
    template = _crop(frame, frame_size, box)
    return frame, frame_size, template, template_size, (0, 0, frame_size[0], frame_size[1])


@pytest.fixture
def matcher() -> runtime.TemplateMatcherRuntime:
    root, _artifact, _manifest = _assert_release_fixture()
    instance = runtime.TemplateMatcherRuntime(root)
    try:
        yield instance
    finally:
        instance.close()


def test_actual_release_provenance_and_source_bindings(matcher: runtime.TemplateMatcherRuntime) -> None:
    _root, _artifact, manifest_path = _release_paths()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert matcher.artifact_sha256 == runtime.EXPECTED_ARTIFACT_SHA256
    assert matcher.source_hashes == runtime.EXPECTED_SOURCE_HASHES
    assert matcher.source_bindings["template-matcher-binary"] == runtime.EXPECTED_ARTIFACT_SHA256
    assert matcher.source_bindings["template-matcher-Cargo.toml"] == manifest["source_hashes"]["Cargo.toml"]
    assert matcher.source_bindings["template-matcher-Cargo.lock"] == manifest["source_hashes"]["Cargo.lock"]
    assert matcher.source_bindings["template-matcher-src/main.rs"] == manifest["source_hashes"]["src/main.rs"]
    source_path = Path(runtime.__file__).resolve()
    assert (
        matcher.source_bindings["template-matcher-runtime.py"]
        == hashlib.sha256(source_path.read_bytes()).hexdigest()
    )


def test_operator_receipt_merges_actual_matcher_bindings(tmp_path: Path) -> None:
    root, artifact, manifest_path = _assert_release_fixture()
    construct_path = tmp_path / "construct.png"
    restore_path = tmp_path / "restore.png"
    Image.new("RGB", (1, 1), (10, 20, 30)).save(construct_path)
    Image.new("RGB", (1, 1), (30, 20, 10)).save(restore_path)
    construct_path.chmod(0o600)
    restore_path.chmod(0o600)
    manifest = operator.TemplateManifest(
        operator.TemplateRole(construct_path, hashlib.sha256(construct_path.read_bytes()).hexdigest(), 5.0),
        operator.TemplateRole(restore_path, hashlib.sha256(restore_path.read_bytes()).hexdigest(), 5.0),
    )
    matcher_runtime = runtime.TemplateMatcherRuntime(root)
    candidate = operator.NativeCalibrationOperator(
        mode=operator.MODE_RECOVER,
        generation=1,
        config_path=tmp_path / "config.toml",
        template_manifest=manifest,
        client=SimpleNamespace(),
        screen=SimpleNamespace(),
        input_device=SimpleNamespace(),
        matcher_runtime=matcher_runtime,
        dependencies={"caller-pin": "a" * 64},
        receipt_root=tmp_path / "receipts",
    )
    try:
        dependencies = candidate.receipt.dependencies
        published_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert dependencies["caller-pin"] == "a" * 64
        assert dependencies["template-matcher-binary"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert (
            dependencies["template-matcher-runtime.py"]
            == hashlib.sha256(Path(runtime.__file__).resolve().read_bytes()).hexdigest()
        )
        for name in ("Cargo.toml", "Cargo.lock", "src/main.rs"):
            assert dependencies[f"template-matcher-{name}"] == published_manifest["source_hashes"][name]
    finally:
        candidate.matcher.close()


def test_search_and_score_at_match_independent_reference(matcher: runtime.TemplateMatcherRuntime) -> None:
    frame, frame_size, template, template_size, region = _request_args()
    deadline = time.monotonic() + 2.0
    expected = _reference(frame, frame_size, template, template_size, region, 1)
    actual = matcher.search(frame, frame_size, template, template_size, region, deadline_monotonic=deadline)
    assert actual == expected

    score_box = expected.box
    expected_score = _reference(frame, frame_size, template, template_size, score_box, 2)
    actual_score = matcher.score_at(
        frame,
        frame_size,
        template,
        template_size,
        score_box,
        deadline_monotonic=deadline,
    )
    assert actual_score == expected_score


def test_tie_threshold_ambiguity_and_exclusive_candidate_shapes(
    matcher: runtime.TemplateMatcherRuntime,
) -> None:
    frame_size = (5, 3)
    frame = bytearray(frame_size[0] * frame_size[1] * 3)
    template_size = (1, 1)
    template = bytes((10, 20, 30))
    for x, y in ((1, 0), (0, 1)):
        offset = (y * frame_size[0] + x) * 3
        frame[offset : offset + 3] = template
    expected = _reference(bytes(frame), frame_size, template, template_size, (0, 0, 3, 2), 1)
    actual = matcher.search(
        bytes(frame),
        frame_size,
        template,
        template_size,
        (0, 0, 3, 2),
        deadline_monotonic=time.monotonic() + 2,
    )
    assert actual == expected
    assert actual.box == (0, 1, 1, 1)
    assert actual.status == "ambiguous"

    threshold_frame = bytes((15, 0, 0))
    threshold_template = bytes((0, 0, 0))
    accepted = matcher.score_at(
        threshold_frame,
        (1, 1),
        threshold_template,
        (1, 1),
        (0, 0, 1, 1),
        deadline_monotonic=time.monotonic() + 2,
    )
    assert accepted.status == "matched" and accepted.sad == 15
    rejected = matcher.score_at(
        bytes((16, 0, 0)),
        (1, 1),
        threshold_template,
        (1, 1),
        (0, 0, 1, 1),
        deadline_monotonic=time.monotonic() + 2,
    )
    assert rejected.status == "rejected" and rejected.sad == 16

    red = bytes((255, 0, 0) * 4)
    blue = bytes((0, 0, 255) * 4)
    panel = bytearray(8 * 2 * 3)
    for row in range(2):
        panel[row * 8 * 3 : row * 8 * 3 + 6] = red[row * 6 : row * 6 + 6]
        panel[row * 8 * 3 + 6 * 3 : row * 8 * 3 + 8 * 3] = blue[row * 6 : row * 6 + 6]
    construct = matcher.search(
        bytes(panel),
        (8, 2),
        red,
        (2, 2),
        (0, 0, 8, 2),
        deadline_monotonic=time.monotonic() + 2,
    )
    restore = matcher.search(
        bytes(panel),
        (8, 2),
        blue,
        (2, 2),
        (0, 0, 8, 2),
        deadline_monotonic=time.monotonic() + 2,
    )
    assert construct.status == restore.status == "matched"
    assert construct.box[0] + construct.box[2] <= restore.box[0]


def _fake_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> runtime.TemplateMatcherRuntime:
    root = tmp_path / "release"
    root.mkdir(mode=0o700, parents=True)
    script = root / runtime.ARTIFACT_NAME
    script.write_text(body, encoding="utf-8")
    script.chmod(0o700)
    _root, _artifact, manifest_path = _release_paths()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    manifest["artifact_sha256"] = digest
    (root / runtime.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    (root / runtime.MANIFEST_NAME).chmod(0o600)
    monkeypatch.setattr(runtime, "EXPECTED_ARTIFACT_SHA256", digest)
    return runtime.TemplateMatcherRuntime(root)


def test_malformed_native_output_is_refused(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    instance = _fake_runtime(
        tmp_path,
        monkeypatch,
        '#!/usr/bin/python3\nimport sys\nsys.stdin.buffer.read()\nsys.stdout.write(\'{\\"status\\":\\"matched\\"}\\n\')\n',
    )
    try:
        with pytest.raises(runtime.MatcherProtocolError):
            instance.score_at(
                bytes(3),
                (1, 1),
                bytes(3),
                (1, 1),
                (0, 0, 1, 1),
                deadline_monotonic=time.monotonic() + 2,
            )
    finally:
        instance.close()


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ('{"status":[],"sad":0,"count":3,"box":[0,0,1,1]}\n', "status is not closed"),
        ('{"status":{},"sad":0,"count":3,"box":[0,0,1,1]}\n', "status is not closed"),
        (
            '{"status":"ambiguous","sad":0,"count":3,"box":[0,0,1,1]}\n',
            "SCORE_AT response cannot be ambiguous",
        ),
    ],
)
def test_owned_malformed_process_reaches_specific_response_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: str,
    message: str,
) -> None:
    body = f"#!/usr/bin/python3\nimport sys\nsys.stdin.buffer.read()\nsys.stdout.write({response!r})\n"
    instance = _fake_runtime(tmp_path, monkeypatch, body)
    try:
        with pytest.raises(runtime.MatcherProtocolError, match=message):
            instance.score_at(
                bytes(3),
                (1, 1),
                bytes(3),
                (1, 1),
                (0, 0, 1, 1),
                deadline_monotonic=time.monotonic() + 2,
            )
    finally:
        instance.close()


@pytest.mark.parametrize("operation", ["private", "source"])
def test_fstat_failure_closes_open_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    path = tmp_path / "fixture"
    path.write_bytes(b"fixture")
    path.chmod(0o700 if operation == "private" else 0o600)
    real_open = os.open
    real_fstat = os.fstat
    opened: list[int] = []

    def tracking_open(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def failing_fstat(_descriptor: int):
        raise OSError("injected fstat failure")

    monkeypatch.setattr(runtime.os, "open", tracking_open)
    monkeypatch.setattr(runtime.os, "fstat", failing_fstat)
    with pytest.raises(runtime.ArtifactError):
        if operation == "private":
            runtime._open_private_file(path, executable=True)
        else:
            runtime._sha256_path(path)
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError):
            real_fstat(descriptor)


def test_expired_deadline_refuses_before_spawn(
    matcher: runtime.TemplateMatcherRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_spawn(*_args, **_kwargs):
        raise AssertionError("expired matcher spawned a process")

    monkeypatch.setattr(runtime.subprocess, "Popen", unexpected_spawn)
    frame, frame_size, template, template_size, region = _request_args()
    with pytest.raises(runtime.MatcherTimeout, match="before spawn"):
        matcher.search(
            frame,
            frame_size,
            template,
            template_size,
            region,
            deadline_monotonic=time.monotonic() - 1,
        )


def test_verified_child_gets_closed_environment_and_neutral_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    for name in (
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "GLIBC_TUNABLES",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONINSPECT",
        "PYTHONSTARTUP",
    ):
        monkeypatch.setenv(name, "ambient-poison")
    body = """#!/usr/bin/python3
import json
import os
import sys

sys.stdin.buffer.read()
if os.getcwd() != "/" or dict(os.environ) != {"LC_ALL": "C"}:
    print(f"unexpected child context: cwd={os.getcwd()!r} env={dict(os.environ)!r}", file=sys.stderr)
    raise SystemExit(17)
sys.stdout.write(json.dumps({"status": "matched", "sad": 0, "count": 12, "box": [0, 0, 2, 2]}) + chr(10))
"""
    real_popen = runtime.subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    def tracked_popen(*args, **kwargs) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(runtime.subprocess, "Popen", tracked_popen)
    baseline_fds = len(os.listdir("/proc/self/fd"))
    instance = _fake_runtime(tmp_path / "actor", monkeypatch, body)
    try:
        frame, frame_size, template, template_size, region = _request_args()
        result = instance.search(
            frame,
            frame_size,
            template,
            template_size,
            region,
            deadline_monotonic=time.monotonic() + 2,
        )
    finally:
        instance.close()
    assert result == runtime.MatcherResult("matched", 0, 12, (0, 0, 2, 2))
    assert len(processes) == 1
    process = processes[0]
    assert process.poll() == 0
    assert process.args[0].startswith("/proc/self/fd/")
    for stream in (process.stdin, process.stdout, process.stderr):
        assert stream is not None
        assert stream.closed
    assert len(os.listdir("/proc/self/fd")) == baseline_fds


def test_timeout_and_cancellation_kill_and_reap_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "#!/usr/bin/python3\nimport sys, time\nsys.stdin.buffer.read()\ntime.sleep(2)\n"
    frame, frame_size, template, template_size, region = _request_args()
    real_popen = runtime.subprocess.Popen
    processes: list[subprocess.Popen[bytes]] = []

    def tracked_popen(*args, **kwargs) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(runtime.subprocess, "Popen", tracked_popen)

    def assert_process_clean(process: subprocess.Popen[bytes]) -> None:
        assert process.poll() is not None
        assert process.returncode is not None
        for stream in (process.stdin, process.stdout, process.stderr):
            assert stream is not None
            assert stream.closed

    baseline_fds = len(os.listdir("/proc/self/fd"))
    instance = _fake_runtime(tmp_path, monkeypatch, body)
    try:
        with pytest.raises(runtime.MatcherTimeout):
            instance.search(
                frame,
                frame_size,
                template,
                template_size,
                region,
                deadline_monotonic=time.monotonic() + 0.5,
            )
    finally:
        instance.close()
    assert len(processes) == 1
    assert_process_clean(processes[0])
    assert len(os.listdir("/proc/self/fd")) == baseline_fds

    instance = _fake_runtime(tmp_path / "cancel", monkeypatch, body)
    token = runtime.CancellationToken()
    outcome: list[BaseException] = []

    def invoke() -> None:
        try:
            instance.search(
                frame,
                frame_size,
                template,
                template_size,
                region,
                deadline_monotonic=time.monotonic() + 2,
                cancel_event=token,
            )
        except BaseException as error:
            outcome.append(error)

    worker = threading.Thread(target=invoke)
    worker.start()
    spawn_deadline = time.monotonic() + 1.0
    while len(processes) < 2 and time.monotonic() < spawn_deadline:
        time.sleep(0.005)
    assert len(processes) == 2
    token.cancel()
    worker.join(1.0)
    instance.close()
    assert not worker.is_alive()
    assert len(outcome) == 1 and isinstance(outcome[0], runtime.MatcherCancelled)
    assert len(processes) == 2
    assert_process_clean(processes[1])
    assert len(os.listdir("/proc/self/fd")) == baseline_fds


def test_verified_fd_survives_binary_path_replacement(tmp_path: Path) -> None:
    _release, artifact, manifest = _assert_release_fixture()
    root = tmp_path / "release"
    root.mkdir(mode=0o700)
    shutil.copy2(artifact, root / runtime.ARTIFACT_NAME)
    shutil.copy2(manifest, root / runtime.MANIFEST_NAME)
    (root / runtime.ARTIFACT_NAME).chmod(0o700)
    (root / runtime.MANIFEST_NAME).chmod(0o600)
    instance = runtime.TemplateMatcherRuntime(root)
    original = root / runtime.ARTIFACT_NAME
    moved = root / "original"
    os.replace(original, moved)
    original.write_bytes(b"replacement is not the reviewed executable")
    original.chmod(0o700)
    try:
        frame, frame_size, template, template_size, region = _request_args()
        result = instance.search(
            frame,
            frame_size,
            template,
            template_size,
            region,
            deadline_monotonic=time.monotonic() + 2,
        )
        assert result.status == _reference(frame, frame_size, template, template_size, region, 1).status
    finally:
        instance.close()


@pytest.mark.parametrize("tamper", ["manifest", "binary", "symlink"])
def test_provenance_tampering_is_refused(tmp_path: Path, tamper: str) -> None:
    _release, artifact, manifest = _assert_release_fixture()
    root = tmp_path / "release"
    root.mkdir(mode=0o700)
    shutil.copy2(artifact, root / runtime.ARTIFACT_NAME)
    shutil.copy2(manifest, root / runtime.MANIFEST_NAME)
    (root / runtime.ARTIFACT_NAME).chmod(0o700)
    (root / runtime.MANIFEST_NAME).chmod(0o600)
    if tamper == "manifest":
        manifest = json.loads((root / runtime.MANIFEST_NAME).read_text(encoding="utf-8"))
        manifest["source_hashes"]["Cargo.toml"] = "0" * 64
        (root / runtime.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
        (root / runtime.MANIFEST_NAME).chmod(0o600)
    elif tamper == "binary":
        (root / runtime.ARTIFACT_NAME).write_bytes(b"tampered")
        (root / runtime.ARTIFACT_NAME).chmod(0o700)
    else:
        (root / runtime.ARTIFACT_NAME).unlink()
        (root / runtime.ARTIFACT_NAME).symlink_to(artifact)
    with pytest.raises(runtime.ArtifactError):
        runtime.TemplateMatcherRuntime(root)
