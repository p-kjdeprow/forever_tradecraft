"""Benchmark the standalone matcher against caller-supplied PNG artifacts."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import platform
import stat
import statistics
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image
from PIL import __version__ as PILLOW_VERSION

from crowley.native import pins
from crowley.native.build import NATIVE_ROOT

ROOT = NATIVE_ROOT
SOURCE_FILES = pins.MATCHER_SOURCE_FILES
SOURCE_PATHS = {
    "Cargo.toml": "template_matcher/Cargo.toml",
    "Cargo.lock": "template_matcher/Cargo.lock",
    "src/main.rs": "template_matcher/src/main.rs",
}
MAGIC = pins.MATCHER_MAGIC
SEARCH = 1
SCORE_AT = 2
ARTIFACT_NAME = pins.MATCHER_ARTIFACT_NAME
MANIFEST_NAME = pins.MATCHER_MANIFEST_NAME


class BenchmarkError(RuntimeError):
    pass


def _regular(path: Path, *, executable: bool = False, private: bool = False) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise BenchmarkError(f"required artifact is unavailable: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise BenchmarkError(f"required artifact is not a regular file: {path}")
    if executable and not info.st_mode & 0o100:
        raise BenchmarkError(f"required artifact is not executable: {path}")
    if private and info.st_mode & 0o077:
        raise BenchmarkError(f"required artifact is not private: {path}")


def _private_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise BenchmarkError(f"required artifact directory is unavailable: {path}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise BenchmarkError(f"required artifact directory is not private: {path}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    _regular(path)
    return _sha256(path.read_bytes())


def _source_binding(source_root: Path, binary: Path) -> tuple[dict[str, str], str, str]:
    binary = binary.absolute()
    _regular(binary, executable=True, private=True)
    if binary.name != ARTIFACT_NAME:
        raise BenchmarkError("binary name is not the reviewed version")
    _private_directory(binary.parent)
    manifest_path = binary.parent / MANIFEST_NAME
    _regular(manifest_path, private=True)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BenchmarkError("binary manifest is malformed") from error
    if not isinstance(manifest, dict):
        raise BenchmarkError("binary manifest is not an object")
    source_hashes = {name: _file_sha256(source_root / SOURCE_PATHS[name]) for name in SOURCE_FILES}
    digest = hashlib.sha256()
    for name in SOURCE_FILES:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source_hashes[name].encode("ascii"))
        digest.update(b"\n")
    source_pin = digest.hexdigest()
    if (
        manifest.get("schema") != "crowley.template-matcher-release"
        or manifest.get("schema_version") != 1
        or manifest.get("artifact") != ARTIFACT_NAME
        or manifest.get("source_hashes") != source_hashes
        or manifest.get("source_sha256") != source_pin
        or manifest.get("source_snapshot_sha256") != source_pin
        or manifest.get("target") != pins.TARGET_TRIPLE
        or manifest.get("workspace_sha256") != pins.MATCHER_WORKSPACE_SHA256
        or manifest.get("artifact_sha256") != _file_sha256(binary)
    ):
        raise BenchmarkError("binary publication does not match current source pins and digest")
    return source_hashes, source_pin, _file_sha256(binary)


def _load_rgb(path: Path) -> tuple[int, int, bytes, str]:
    _regular(path)
    raw = path.read_bytes()
    try:
        with Image.open(io.BytesIO(raw)) as image_raw:
            image = image_raw.convert("RGB")
            return image.width, image.height, image.tobytes(), _sha256(raw)
    except (OSError, ValueError) as error:
        raise BenchmarkError(f"image cannot be decoded: {path}") from error


def _payload(
    operation: int,
    frame_width: int,
    frame_height: int,
    template_width: int,
    template_height: int,
    region: tuple[int, int, int, int],
    frame: bytes,
    template: bytes,
) -> bytes:
    return (
        struct.pack(
            "<8s10I",
            MAGIC,
            operation,
            0,
            frame_width,
            frame_height,
            template_width,
            template_height,
            *region,
        )
        + frame
        + template
    )


def _reference(
    frame_width: int,
    template_width: int,
    template_height: int,
    region: tuple[int, int, int, int],
    frame: bytes,
    template: bytes,
    operation: int,
) -> dict[str, Any]:
    region_x, region_y, region_width, region_height = region
    count = len(template)

    def sad_at(x: int, y: int) -> int:
        total = 0
        row_bytes = template_width * 3
        for row in range(template_height):
            frame_start = ((y + row) * frame_width + x) * 3
            template_start = row * row_bytes
            total += sum(
                abs(frame[frame_start + offset] - template[template_start + offset])
                for offset in range(row_bytes)
            )
        return total

    if operation == SCORE_AT:
        x, y = region_x, region_y
        sad = sad_at(x, y)
        return {
            "status": "matched" if sad <= 5 * count else "rejected",
            "sad": sad,
            "count": count,
            "box": [x, y, template_width, template_height],
        }

    candidates = [
        (sad_at(x, y), x, y)
        for y in range(region_y, region_y + region_height - template_height + 1)
        for x in range(region_x, region_x + region_width - template_width + 1)
    ]
    best = min(candidates)
    accepted = [candidate for candidate in candidates if candidate[0] <= 5 * count]
    _, x, y = best
    ambiguous = any(
        (other_x, other_y) != (x, y)
        and not (
            x < other_x + template_width
            and other_x < x + template_width
            and y < other_y + template_height
            and other_y < y + template_height
        )
        for _, other_x, other_y in accepted
    )
    return {
        "status": "ambiguous" if ambiguous else "matched" if best[0] <= 5 * count else "rejected",
        "sad": best[0],
        "count": count,
        "box": [x, y, template_width, template_height],
    }


def _invoke(binary: Path, payload: bytes) -> tuple[float, float, float, dict[str, Any]]:
    started = time.perf_counter_ns()
    result = subprocess.run(
        [str(binary)],
        input=payload,
        capture_output=True,
        check=False,
        cwd=Path("/"),
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    received = time.perf_counter_ns()
    if result.returncode != 0 or result.stderr:
        raise BenchmarkError(f"matcher invocation failed: {result.stderr[:200]!r}")
    try:
        parsed = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise BenchmarkError("matcher result is not JSON") from error
    finished = time.perf_counter_ns()
    if not isinstance(parsed, dict):
        raise BenchmarkError("matcher result is not an object")
    return (
        (finished - started) / 1e6,
        (received - started) / 1e6,
        (finished - received) / 1e6,
        parsed,
    )


def _median(values: list[float]) -> float:
    return round(statistics.median(values), 4)


def benchmark(
    binary: Path,
    frame_path: Path,
    construct_path: Path,
    restore_path: Path,
    region: tuple[int, int, int, int],
    source_root: Path,
    samples: int,
    warmups: int,
) -> dict[str, Any]:
    binary = binary.absolute()
    source_hashes, source_pin, binary_sha256_before = _source_binding(source_root, binary)
    frame_width, frame_height, frame, frame_sha = _load_rgb(frame_path)
    template_data = {}
    for role, path in (("construct", construct_path), ("restore", restore_path)):
        width, height, data, digest = _load_rgb(path)
        template_data[role] = {"width": width, "height": height, "data": data, "sha256": digest}
    region_x, region_y, region_width, region_height = region
    if (
        region_x < 0
        or region_y < 0
        or region_width <= 0
        or region_height <= 0
        or region_x + region_width > frame_width
        or region_y + region_height > frame_height
    ):
        raise BenchmarkError("region is outside the frame")
    if (template_data["construct"]["width"], template_data["construct"]["height"]) != (
        template_data["restore"]["width"],
        template_data["restore"]["height"],
    ):
        raise BenchmarkError("composed SCORE_AT shapes require equal template dimensions")
    template_width = template_data["construct"]["width"]
    template_height = template_data["construct"]["height"]
    if template_width > region_width or template_height > region_height:
        raise BenchmarkError("template is larger than the region")

    def make(role: str, operation: int, box: tuple[int, int, int, int]) -> bytes:
        template = template_data[role]
        return _payload(
            operation,
            frame_width,
            frame_height,
            template["width"],
            template["height"],
            box,
            frame,
            template["data"],
        )

    search_payloads = {role: make(role, SEARCH, region) for role in ("construct", "restore")}
    search_expected = {
        role: _reference(
            frame_width,
            template_width,
            template_height,
            region,
            frame,
            template_data[role]["data"],
            SEARCH,
        )
        for role in ("construct", "restore")
    }
    search_observed = {role: _invoke(binary, search_payloads[role])[3] for role in ("construct", "restore")}
    if search_observed != search_expected:
        raise BenchmarkError(f"reference mismatch in SEARCH: {search_expected!r} != {search_observed!r}")
    boxes = {role: tuple(search_observed[role]["box"]) for role in ("construct", "restore")}
    score_payloads = {
        role: {
            "self": make(role, SCORE_AT, boxes[role]),
            "rival": make("restore" if role == "construct" else "construct", SCORE_AT, boxes[role]),
        }
        for role in ("construct", "restore")
    }
    score_expected = {
        role: {
            "self": _reference(
                frame_width,
                template_data[role]["width"],
                template_data[role]["height"],
                boxes[role],
                frame,
                template_data[role]["data"],
                SCORE_AT,
            ),
            "rival": _reference(
                frame_width,
                template_data["restore" if role == "construct" else "construct"]["width"],
                template_data["restore" if role == "construct" else "construct"]["height"],
                boxes[role],
                frame,
                template_data["restore" if role == "construct" else "construct"]["data"],
                SCORE_AT,
            ),
        }
        for role in ("construct", "restore")
    }
    for role in ("construct", "restore"):
        for shape in ("self", "rival"):
            observed = _invoke(binary, score_payloads[role][shape])[3]
            if observed != score_expected[role][shape]:
                raise BenchmarkError("reference mismatch in SCORE_AT")

    preparation = []
    for _ in range(warmups):
        _load_rgb(frame_path)
        _load_rgb(construct_path)
        _load_rgb(restore_path)
    for _ in range(samples):
        started = time.perf_counter_ns()
        _load_rgb(frame_path)
        _load_rgb(construct_path)
        _load_rgb(restore_path)
        _payload(
            SEARCH,
            frame_width,
            frame_height,
            template_width,
            template_height,
            region,
            frame,
            template_data["construct"]["data"],
        )
        _payload(
            SEARCH,
            frame_width,
            frame_height,
            template_width,
            template_height,
            region,
            frame,
            template_data["restore"]["data"],
        )
        preparation.append((time.perf_counter_ns() - started) / 1e6)

    role_timings: dict[str, Any] = {}
    for role in ("construct", "restore"):
        for _ in range(warmups):
            _invoke(binary, search_payloads[role])
            _invoke(binary, score_payloads[role]["self"])
            _invoke(binary, score_payloads[role]["rival"])
        search_runs = [_invoke(binary, search_payloads[role]) for _ in range(samples)]
        score_self_runs = [_invoke(binary, score_payloads[role]["self"]) for _ in range(samples)]
        score_rival_runs = [_invoke(binary, score_payloads[role]["rival"]) for _ in range(samples)]
        search = [run[0] for run in search_runs]
        score_self = [run[0] for run in score_self_runs]
        score_rival = [run[0] for run in score_rival_runs]
        role_timings[role] = {
            "box": list(boxes[role]),
            "search_ms": {
                "samples": [round(value, 4) for value in search],
                "median": _median(search),
                "startup_ipc_native_scoring_ms": {
                    "samples": [round(run[1], 4) for run in search_runs],
                    "median": _median([run[1] for run in search_runs]),
                },
                "json_parse_ms": {
                    "samples": [round(run[2], 4) for run in search_runs],
                    "median": _median([run[2] for run in search_runs]),
                },
            },
            "score_at_self_ms": {
                "samples": [round(value, 4) for value in score_self],
                "median": _median(score_self),
                "startup_ipc_native_scoring_ms": {
                    "samples": [round(run[1], 4) for run in score_self_runs],
                    "median": _median([run[1] for run in score_self_runs]),
                },
                "json_parse_ms": {
                    "samples": [round(run[2], 4) for run in score_self_runs],
                    "median": _median([run[2] for run in score_self_runs]),
                },
            },
            "score_at_rival_ms": {
                "samples": [round(value, 4) for value in score_rival],
                "median": _median(score_rival),
                "startup_ipc_native_scoring_ms": {
                    "samples": [round(run[1], 4) for run in score_rival_runs],
                    "median": _median([run[1] for run in score_rival_runs]),
                },
                "json_parse_ms": {
                    "samples": [round(run[2], 4) for run in score_rival_runs],
                    "median": _median([run[2] for run in score_rival_runs]),
                },
            },
            "search_result": search_observed[role],
            "score_at_self_result": score_expected[role]["self"],
            "score_at_rival_result": score_expected[role]["rival"],
        }

    exclusive = []
    dual_positive = []
    for _ in range(warmups):
        _invoke(binary, search_payloads["construct"])
        _invoke(binary, score_payloads["construct"]["rival"])
        _invoke(binary, search_payloads["construct"])
        _invoke(binary, search_payloads["restore"])
    for _ in range(samples):
        started = time.perf_counter_ns()
        first = _invoke(binary, search_payloads["construct"])[3]
        second = _invoke(binary, score_payloads["construct"]["rival"])[3]
        exclusive.append((time.perf_counter_ns() - started) / 1e6)
        if (first, second) != (
            search_expected["construct"],
            score_expected["construct"]["rival"],
        ):
            raise BenchmarkError("reference mismatch in exclusive composition")
        started = time.perf_counter_ns()
        first = _invoke(binary, search_payloads["construct"])[3]
        second = _invoke(binary, search_payloads["restore"])[3]
        dual_positive.append((time.perf_counter_ns() - started) / 1e6)
        if (first, second) != (search_expected["construct"], search_expected["restore"]):
            raise BenchmarkError("reference mismatch in dual-positive composition")

    binary_sha256_after = _file_sha256(binary)
    if binary_sha256_after != binary_sha256_before:
        raise BenchmarkError("binary changed during benchmark")

    return {
        "schema": "crowley.template-matcher-benchmark",
        "schema_version": 1,
        "qualification": "caller-supplied image artifacts; computation evidence only",
        "protocol": {
            "magic": "CRWTM001",
            "header_bytes": 48,
            "frame": [frame_width, frame_height],
            "template": [template_width, template_height],
            "region": list(region),
        },
        "artifact": {
            "name": binary.name,
            "sha256": binary_sha256_before,
            "sha256_before": binary_sha256_before,
            "sha256_after": binary_sha256_after,
            "source_sha256": source_pin,
            "source_hashes": source_hashes,
        },
        "inputs": {
            "frame_sha256": frame_sha,
            "construct_sha256": template_data["construct"]["sha256"],
            "restore_sha256": template_data["restore"]["sha256"],
        },
        "preparation_ms": {
            "samples": [round(value, 4) for value in preparation],
            "median": _median(preparation),
            "scope": "PNG decode + RGB conversion + framing and serialization",
        },
        "roles": role_timings,
        "composed": {
            "exclusive_ms": {
                "samples": [round(value, 4) for value in exclusive],
                "median": _median(exclusive),
                "shape": "SEARCH construct + SCORE_AT restore at construct box",
            },
            "dual_positive_ms": {
                "samples": [round(value, 4) for value in dual_positive],
                "median": _median(dual_positive),
                "shape": "SEARCH construct + SEARCH restore",
            },
        },
        "interval_scope": (
            "SEARCH/SCORE_AT/composed include process startup + full stdin/stdout IPC + "
            "native scoring + JSON parse"
        ),
        "samples": samples,
        "warmups": warmups,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--frame", type=Path, required=True)
    parser.add_argument("--construct", type=Path, required=True)
    parser.add_argument("--restore", type=Path, required=True)
    parser.add_argument("--region", type=int, nargs=4, metavar=("X", "Y", "WIDTH", "HEIGHT"), required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--samples", type=int, default=8)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100 or not 0 <= args.warmups <= 20:
        parser.error("samples must be 1..100 and warmups must be 0..20")
    try:
        binary = args.binary.absolute()
        report = benchmark(
            binary,
            args.frame.resolve(),
            args.construct.resolve(),
            args.restore.resolve(),
            tuple(args.region),
            args.source_root.resolve(),
            args.samples,
            args.warmups,
        )
        report["runner"] = {
            "argv": list(sys.argv),
            "python": platform.python_version(),
            "pillow": PILLOW_VERSION,
            "sha256": _file_sha256(Path(__file__).absolute()),
        }
    except BenchmarkError as error:
        print(f"benchmark_template_matcher: {error}", file=sys.stderr)
        return 1
    encoded = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.write_text(encoded, encoding="utf-8")
        args.output.chmod(0o600)
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
