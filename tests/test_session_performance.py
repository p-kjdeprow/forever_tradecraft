from __future__ import annotations

import binascii
import hashlib
import json
import os
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image

import tools.session_performance as performance
from crowley.session.decoder import PixelDecoder
from crowley.session.protocol import CAPACITY, HEADER_SIZE, ProtocolError, pack_cells
from tests.wow_harness import WowClient
from tools.session_performance import (
    MAX_INPUTS,
    MAX_LIVE_RECEIPT_BYTES,
    MAX_PNG_BYTES,
    MAX_RECEIPT_BYTES,
    MAX_RECEIPTS,
    PerformanceInputError,
    _stage_summary,
    build_live_report,
    build_report,
)


def _lua_fixture_image() -> Image.Image:
    """Render the actual Lua CRS1 fixture through the shared addon harness."""

    client = WowClient()
    client.set_canonical_plan()
    client.login()
    client.publish_agent()
    return client.render_agent_capture()


def _write_status_png(
    path: Path,
    *,
    mutate: tuple[int, int, tuple[int, int, int]] | None = None,
) -> None:
    image = _lua_fixture_image()
    try:
        if mutate is not None:
            x, y, color = mutate
            image.putpixel((x, y), color)
        image.save(path, format="PNG")
    finally:
        image.close()


def _write_frame_png(path: Path, frame: bytes) -> None:
    rows = pack_cells(frame)
    image = Image.new("RGB", (256, 256), (0, 0, 0))
    pixels = image.load()
    for row, bits in enumerate(rows):
        for column, bit in enumerate(bits):
            if bit:
                color = (255, 255, 255)
                for dy in range(2):
                    for dx in range(2):
                        pixels[column * 2 + dx, row * 2 + dy] = color
    image.save(path, format="PNG")
    image.close()


def _lua_frame() -> bytes:
    client = WowClient()
    client.set_canonical_plan()
    client.login()
    client.publish_agent()
    return client.agent_frame


def test_stage_summary_uses_ceil_quantiles_and_preserves_samples() -> None:
    summary = _stage_summary([0.01, 0.02, 0.03, 0.04], [0.001, 0.002, 0.003, 0.004])

    assert summary["wall_p95_ms"] == pytest.approx(40.0)
    assert summary["cpu_p95_ms"] == pytest.approx(4.0)
    assert summary["wall_samples_seconds"] == [0.01, 0.02, 0.03, 0.04]


def test_real_lua_crs1_fixture_decodes_at_public_boundary() -> None:
    image = _lua_fixture_image()
    try:
        sample = PixelDecoder().decode(image, monotonic=1.0, window_identity="lua-fixture")
    finally:
        image.close()
    assert sample.kind == "FRESH"
    assert sample.frame is not None
    assert sample.frame.snapshot.value["schema"] == 6


def test_offline_report_measures_public_decode_and_pure_planners(tmp_path: Path) -> None:
    input_path = tmp_path / "status.png"
    _write_status_png(input_path)

    report = build_report([input_path], samples=2, warmups=1)

    assert report["schema"] == "crowley.runtime-performance-decision"
    assert set(report) == {
        "schema",
        "schema_version",
        "accepted_source_hashes",
        "producer_binding",
        "config_binding",
        "runs",
        "statistics",
        "decision",
        "reason_codes",
        "limitations",
    }
    assert report["decision"] == "insufficient_evidence"
    assert "no_preserved_timing_receipts" in report["reason_codes"]
    measurement = report["statistics"]["offline_measurements"][0]
    assert measurement["input_sha256"]
    assert measurement["input_dimensions"] == [256, 256]
    assert measurement["failures"] == []
    for name in (
        "png_load",
        "full_strict_decode",
        "pointer_planning",
        "click_planning",
        "key_planning",
        "camera_planning",
    ):
        assert measurement["stages"][name]["samples"] == 2
    for name in (
        "pixel_extract_2x2_validation",
        "cell_unpack",
        "frame_inflate_json_schema_validation",
    ):
        stage = measurement["stages"][name]
        assert stage["samples"] is None
        assert stage["wall_samples_seconds"] is None
        assert stage["cpu_samples_seconds"] is None
        assert stage["unmeasured_reason"]
    assert report["statistics"]["environment"]["python"]


def test_pixel_disagreement_is_refused_by_public_decoder(tmp_path: Path) -> None:
    path = tmp_path / "bad-pixel.png"
    _write_status_png(path, mutate=(0, 0, (127, 127, 127)))
    with Image.open(path) as image:
        image.load()
        with pytest.raises(ProtocolError, match="neither black nor white"):
            PixelDecoder().decode(image, monotonic=1.0, window_identity="lua-fixture")


def test_physical_pixel_disagreement_is_refused_by_public_decoder(tmp_path: Path) -> None:
    path = tmp_path / "physical-pixel.png"
    image = _lua_fixture_image()
    try:
        original = image.getpixel((0, 0))[0]
        opposite = (255, 255, 255) if original < 128 else (0, 0, 0)
        image.putpixel((1, 0), opposite)
        image.save(path, format="PNG")
    finally:
        image.close()
    with Image.open(path) as image:
        image.load()
        with pytest.raises(ProtocolError, match="physical pixels disagree"):
            PixelDecoder().decode(image, monotonic=1.0, window_identity="lua-fixture")


def test_public_decoder_exercises_sequence_and_boot_guards() -> None:
    client = WowClient()
    client.set_canonical_plan()
    client.login()
    client.publish_agent()
    first_image = client.render_agent_capture()
    client.update_agent()
    client.publish_agent()
    second_image = client.render_agent_capture()
    other = WowClient()
    other.set_canonical_plan()
    other.login()
    other.publish_agent()
    other_image = other.render_agent_capture()
    try:
        decoder = PixelDecoder()
        first = decoder.decode(first_image, monotonic=1.0, window_identity="lua-fixture")
        second = decoder.decode(second_image, monotonic=2.0, window_identity="lua-fixture")
        assert first.kind == second.kind == "FRESH"
        assert second.frame is not None and first.frame is not None
        assert second.frame.sequence > first.frame.sequence
        with pytest.raises(ProtocolError, match="sequence is stale"):
            decoder.decode(first_image, monotonic=3.0, window_identity="lua-fixture")
        assert other_image.getpixel((0, 0)) in {(0, 0, 0), (255, 255, 255)}
        with pytest.raises(ProtocolError, match="unexpected status boot change"):
            decoder.decode(other_image, monotonic=4.0, window_identity="lua-fixture")
        decoder.allow_boot_change_once()
        boot = decoder.decode(other_image, monotonic=5.0, window_identity="lua-fixture")
        assert boot.kind == "FRESH"
        assert boot.frame is not None and boot.frame.boot != second.frame.boot
    finally:
        first_image.close()
        second_image.close()
        other_image.close()


def test_public_decoder_resets_boot_epoch_only_for_passive_startup() -> None:
    client = WowClient()
    client.set_canonical_plan()
    client.login()
    client.publish_agent()
    first_image = client.render_agent_capture()
    other = WowClient()
    other.set_canonical_plan()
    other.login()
    other.publish_agent()
    other_image = other.render_agent_capture()
    try:
        decoder = PixelDecoder()
        decoder.decode(first_image, monotonic=1.0, window_identity="lua-fixture")
        with pytest.raises(ProtocolError, match="unexpected status boot change"):
            decoder.decode(other_image, monotonic=2.0, window_identity="lua-fixture")

        decoder.reset_for_passive_startup()
        fresh = decoder.decode(other_image, monotonic=3.0, window_identity="lua-fixture")
        assert fresh.kind == "FRESH"
        assert fresh.frame is not None

        with pytest.raises(ProtocolError, match="unexpected status boot change"):
            decoder.decode(first_image, monotonic=4.0, window_identity="lua-fixture")
    finally:
        first_image.close()
        other_image.close()


def test_crc_valid_schema_refusal_reaches_public_decoder_guard(tmp_path: Path) -> None:
    frame = _lua_frame()
    payload_length = struct.unpack_from("<H", frame, 8)[0]
    raw = zlib.decompress(frame[HEADER_SIZE : HEADER_SIZE + payload_length], -15)
    snapshot = json.loads(raw)
    snapshot["schema"] = 2
    compressor = zlib.compressobj(wbits=-15)
    payload = compressor.compress(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode())
    payload += compressor.flush()
    header = frame[:8] + struct.pack("<H", len(payload)) + frame[10:22]
    crc = struct.pack("<I", binascii.crc32(header + payload) & 0xFFFFFFFF)
    malformed = header + crc + payload + bytes(CAPACITY - len(header) - len(crc) - len(payload))
    path = tmp_path / "schema.png"
    _write_frame_png(path, malformed)
    with Image.open(path) as image:
        image.load()
        with pytest.raises(ProtocolError, match="schema"):
            PixelDecoder().decode(image, monotonic=1.0, window_identity="lua-fixture")


def test_crc_refusal_is_retained_as_a_decode_failure(tmp_path: Path) -> None:
    frame = bytearray(_lua_frame())
    frame[22] ^= 1
    path = tmp_path / "bad-crc.png"
    _write_frame_png(path, bytes(frame))

    report = build_report([path], samples=1, warmups=0)

    measurement = report["statistics"]["offline_measurements"][0]
    assert measurement["stages"]["png_load"]["samples"] == 1
    assert measurement["stages"]["full_strict_decode"]["samples"] == 0
    assert measurement["stages"]["pixel_extract_2x2_validation"]["samples"] is None
    assert measurement["failures"] == [{"sample": 0, "phase": "decode", "error_type": "ProtocolError"}]


def test_invalid_container_and_dimensions_are_refused_before_timing(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.png"
    invalid.write_bytes(b"not a PNG")
    with pytest.raises(PerformanceInputError, match="PNG container"):
        build_report([invalid], samples=1, warmups=0)

    wrong_dimensions = tmp_path / "wrong-dimensions.png"
    Image.new("RGB", (257, 256), (0, 0, 0)).save(wrong_dimensions, format="PNG")
    with pytest.raises(PerformanceInputError, match="dimensions"):
        build_report([wrong_dimensions], samples=1, warmups=0)


def test_input_drift_is_refused_before_a_sample_is_timed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "status.png"
    _write_status_png(path)
    original_load = performance._load_png
    calls = 0

    def change_after_preflight(candidate: Path, **kwargs: object) -> Image.Image:
        nonlocal calls
        image = original_load(candidate, **kwargs)
        calls += 1
        if calls == 1:
            changed = _lua_fixture_image()
            try:
                changed.putpixel((0, 0), (127, 127, 127))
                changed.save(candidate, format="PNG")
            finally:
                changed.close()
        return image

    monkeypatch.setattr(performance, "_load_png", change_after_preflight)
    with pytest.raises(PerformanceInputError, match="changed since preflight"):
        build_report([path], samples=1, warmups=0)


def test_bounds_reject_inputs_receipts_and_oversized_png(tmp_path: Path) -> None:
    path = tmp_path / "status.png"
    _write_status_png(path)
    with pytest.raises(PerformanceInputError, match="inputs"):
        build_report([path] * (MAX_INPUTS + 1), samples=1, warmups=0)
    oversized = tmp_path / "oversized.png"
    oversized.write_bytes(b"x" * (MAX_PNG_BYTES + 1))
    with pytest.raises(PerformanceInputError, match="PNG input size"):
        build_report([oversized], samples=1, warmups=0)
    with pytest.raises(PerformanceInputError, match="receipts"):
        build_report([path], [path] * (MAX_RECEIPTS + 1), samples=1, warmups=0)


def test_fabricated_accepted_receipt_is_diagnostic_only(tmp_path: Path) -> None:
    input_path = tmp_path / "status.png"
    _write_status_png(input_path)
    receipt_path = tmp_path / "private-run-identity.json"
    receipt_path.write_text(
        json.dumps(
            {
                "schema": "diagnostic-receipt",
                "boundaries": {"known": True},
                "unreviewed_user_field": "omit",
                "accepted": True,
                "accepted_source_hashes": {"fake": "source"},
                "producer_binding": {"path": "private"},
                "config_binding": {"character": "redacted"},
                "runs": [{"run_id": "private-run-identity", "samples": []}],
            }
        ),
        encoding="utf-8",
    )

    report = build_report([input_path], [receipt_path], samples=1, warmups=0)

    assert report["decision"] == "insufficient_evidence"
    assert report["accepted_source_hashes"] == {}
    assert report["producer_binding"] is None
    assert report["config_binding"] is None
    assert report["runs"] == []
    diagnostic = report["statistics"]["receipt_diagnostics"][0]
    assert diagnostic["status"] == "diagnostic_only"
    assert diagnostic["reason_code"] == "receipt_contract_unavailable"
    assert diagnostic["known_fields"] == ["boundaries", "schema"]
    assert diagnostic["unknown_field_count"] == 6
    encoded = json.dumps(report, sort_keys=True)
    assert "private-run-identity" not in encoded
    assert "redacted" not in encoded


def test_receipt_descriptor_read_refuses_growth_before_capped_read(tmp_path: Path) -> None:
    path = tmp_path / "growing.json"
    path.write_bytes(b"{}")

    class GrowingReceipt:
        def stat(self):
            return path.stat()

        def open(self, mode: str):
            assert mode == "rb"
            path.write_bytes(b"{}" + b"x" * MAX_RECEIPT_BYTES)
            return path.open(mode)

    diagnostic = performance._receipt_diagnostic(GrowingReceipt())
    assert diagnostic == {"status": "refused", "reason_code": "receipt_changed_during_read"}


def test_deeply_nested_receipt_has_bounded_json_refusal(tmp_path: Path) -> None:
    path = tmp_path / "deep.json"
    path.write_text("[" * 10000 + "]" * 10000, encoding="utf-8")
    diagnostic = performance._receipt_diagnostic(path)
    assert diagnostic["status"] == "refused"
    assert diagnostic["reason_code"] == "receipt_json_nesting_exceeded"


def test_no_receipt_metadata_is_explicitly_null_or_empty(tmp_path: Path) -> None:
    input_path = tmp_path / "status.png"
    _write_status_png(input_path)
    report = build_report([input_path], samples=1, warmups=0)

    assert report["accepted_source_hashes"] == {}
    assert report["producer_binding"] is None
    assert report["config_binding"] is None
    stats = report["statistics"]
    assert stats["pooled_application_compute_cpu_p95_seconds"] is None
    assert stats["pooled_end_to_end_wall_p95_seconds"] is None
    assert stats["ratio_of_quantiles"] is None


def _live_binding() -> dict[str, object]:
    return {
        "schema": "crowley.runtime-performance-accepted-binding",
        "schema_version": 1,
        "accepted_source_hashes": {
            name: hashlib.sha256(name.encode()).hexdigest() for name in performance.LIVE_SOURCE_FILES
        },
        "producer_binding": {
            "binary_sha256": hashlib.sha256(b"producer").hexdigest(),
            "protocol_branch": "ScreenshotArea",
        },
        "config_binding": {
            "raw_file_sha256": hashlib.sha256(b"raw-config").hexdigest(),
            "semantic_sha256": hashlib.sha256(b"semantic-config").hexdigest(),
        },
    }


def _failure(phase: str, reason: str) -> dict[str, str]:
    return {"phase": phase, "reason_code": reason}


def _live_attempt(
    index: int,
    target: float,
    binding: dict[str, object],
    *,
    wall: float = 0.5,
    cpu: float = 0.04,
    state: str = "completed",
    failure: dict[str, str] | None = None,
    model_delta: int = 0,
) -> dict[str, object]:
    outer = target + 0.01
    status = outer + 0.002
    ipc_request = status + 0.001
    owner_entered = ipc_request + 0.001
    request = owner_entered + 0.001
    lock = request + 0.001
    ack = lock + 0.001
    capture_start = ack + 0.001
    capture_end = capture_start + 0.006
    response = capture_end + 0.001
    ipc_return = response + 0.001
    publication = ipc_return + 0.001
    load_start = publication + 0.001
    load_end = load_start + 0.001
    decode_start = load_end + 0.001
    decode_end = outer + wall
    complete = decode_end + 0.001
    counts_before = {"started": 0, "succeeded": 0, "failed": 0}
    counts_after = {"started": 0, "succeeded": 0, "failed": 0}
    counts_delta = {"started": 0, "succeeded": 0, "failed": 0}
    if model_delta:
        counts_after = {"started": 1, "succeeded": 1, "failed": 0}
        counts_delta = {"started": 1, "succeeded": 1, "failed": 0}
    if state == "failed" and failure is None:
        failure = _failure("deadline", "sample_deadline_missed")
    owner_failure = (
        failure
        if failure is not None
        and failure["phase"]
        in {
            "owner_ipc",
            "owner_capture",
            "source_binding",
            "config_binding",
            "producer_binding",
            "model_counter",
            "cleanup",
            "process_termination",
        }
        else None
    )
    generation = 9
    owner = {
        "owner_entered_monotonic": owner_entered,
        "request_started_monotonic": request,
        "lock_acquired_monotonic": lock,
        "ack_sent_monotonic": ack,
        "capture_started_monotonic": capture_start,
        "capture_returned_monotonic": capture_end,
        "response_ready_monotonic": response,
        "receipt_nonce": f"private-nonce-{target:.9f}",
        "generation": generation,
        "source_binding": binding["accepted_source_hashes"],
        "producer_binding": binding["producer_binding"],
        "config_binding": binding["config_binding"],
        "model_calls_before": counts_before,
        "model_calls_after": counts_after,
        "model_call_delta": counts_delta,
        "cleanup": {
            "capture_process_reaped": True,
            "attempt_retired": True,
            "lease_held": True,
            "watchdog_healthy": True,
            "active_process_count": 0,
            "active_process_group_count": 0,
            "observer_process_group_count": 0,
        },
        "failure": owner_failure,
    }
    return {
        "index": index,
        "target_monotonic": target,
        "deadline_monotonic": target + 1.0,
        "outer_started_monotonic": outer,
        "state": state,
        "status_returned_monotonic": status,
        "owner_ipc_request_started_monotonic": ipc_request,
        "owner": owner,
        "owner_ipc_returned_monotonic": ipc_return,
        "png_publication_end_monotonic": publication,
        "png_load_started_monotonic": load_start,
        "png_load_finished_monotonic": load_end,
        "strict_decode_started_monotonic": decode_start,
        "strict_decode_finished_monotonic": decode_end,
        "sample_completed_monotonic": complete,
        "application_compute_cpu_seconds": cpu,
        "application_compute_cpu_method": "time.process_time_ns",
        "partial_decode_wall_seconds": None,
        "partial_decode_cpu_seconds": None,
        "png_sha256": hashlib.sha256(f"png-{index}".encode()).hexdigest(),
        "generation": generation,
        "identity": {
            "fingerprint": "private-identity",
            "boot": "private-boot",
            "sequence": index + 1,
        },
        "failure": failure,
    }


def _live_receipt(
    run_number: int,
    binding: dict[str, object],
    *,
    walls: list[float] | None = None,
    cpus: list[float] | None = None,
    failed_last: bool = False,
    model_delta_at: int | None = None,
    interval_seconds: float = 1.0,
) -> dict[str, object]:
    targets = [100.0 + run_number * 10 + index * interval_seconds for index in range(8)]
    attempts: list[dict[str, object]] = []
    for index, target in enumerate(targets):
        state = "failed" if failed_last and index == 7 else "completed"
        failure = None
        if state == "failed":
            failure = _failure("deadline", "sample_deadline_missed")
        if model_delta_at == index:
            state = "failed"
            failure = _failure("model_counter", "model_call_delta_nonzero")
        attempts.append(
            _live_attempt(
                index,
                target,
                binding,
                wall=walls[index] if walls else 0.5,
                cpu=cpus[index] if cpus else 0.04,
                state=state,
                failure=failure,
                model_delta=1 if model_delta_at == index else 0,
            )
        )
        if state == "failed":
            attempts = attempts[: index + 1]
            break
    run_state = "failed" if any(attempt["state"] == "failed" for attempt in attempts) else "passed"
    return {
        "schema": "crowley.paused-region-performance-run",
        "schema_version": 2,
        "run_id": f"private-run-{run_number}",
        "run_state": run_state,
        "generation": 9,
        "targets_monotonic": targets,
        "observer_source_hashes_before": binding["accepted_source_hashes"],
        "observer_source_hashes_after": binding["accepted_source_hashes"],
        "producer_binding": binding["producer_binding"],
        "config_binding": binding["config_binding"],
        "attempts": attempts,
    }


def _shift_attempt_timestamps(attempt: dict[str, object], delta: float) -> None:
    clock_keys = (
        "outer_started_monotonic",
        "status_returned_monotonic",
        "owner_ipc_request_started_monotonic",
        "owner_ipc_returned_monotonic",
        "png_publication_end_monotonic",
        "png_load_started_monotonic",
        "png_load_finished_monotonic",
        "strict_decode_started_monotonic",
        "strict_decode_finished_monotonic",
        "sample_completed_monotonic",
    )
    for key in clock_keys:
        if attempt[key] is not None:
            attempt[key] += delta
    owner = attempt["owner"]
    if owner is not None:
        for key in (
            "owner_entered_monotonic",
            "request_started_monotonic",
            "lock_acquired_monotonic",
            "ack_sent_monotonic",
            "capture_started_monotonic",
            "capture_returned_monotonic",
            "response_ready_monotonic",
        ):
            if owner[key] is not None:
                owner[key] += delta


def _write_binding(path: Path, binding: dict[str, object]) -> str:
    raw = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def _write_live_receipt(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def _evaluate_live(
    directory: Path,
    values: list[dict[str, object]],
    binding: dict[str, object] | None = None,
) -> dict[str, object]:
    directory.mkdir(parents=True, exist_ok=True)
    accepted = binding or _live_binding()
    binding_path = directory / "accepted-binding.json"
    digest = _write_binding(binding_path, accepted)
    receipts: list[Path] = []
    for index, value in enumerate(values):
        path = directory / f"run-{index}.json"
        _write_live_receipt(path, value)
        receipts.append(path)
    return build_live_report(receipts, accepted_binding=binding_path, binding_sha256=digest)


def test_live_contract_binds_source_config_producer_and_preserves_privacy(tmp_path: Path) -> None:
    binding = _live_binding()
    report = _evaluate_live(tmp_path, [_live_receipt(1, binding)], binding)

    assert report["decision"] == "insufficient_evidence"
    assert set(report) == {
        "schema",
        "schema_version",
        "accepted_source_hashes",
        "producer_binding",
        "config_binding",
        "runs",
        "statistics",
        "decision",
        "reason_codes",
        "limitations",
    }
    run = report["runs"][0]
    assert run["eligible"] is True
    assert run["attempts_begun"] == 8
    assert len(run["completed_pairs"]) == 8
    assert report["accepted_source_hashes"] == binding["accepted_source_hashes"]
    encoded = json.dumps(report, sort_keys=True)
    assert "private-run-1" not in encoded
    assert "private-nonce" not in encoded
    assert "private-identity" not in encoded
    assert "private-boot" not in encoded
    assert "accepted-binding.json" not in encoded
    assert "png-0" not in encoded
    attempt = run["attempts"][0]
    assert attempt["intervals"]["end_to_end_wall_seconds"] == pytest.approx(0.5)
    assert attempt["intervals"]["external_capture_parent_validation_wall_seconds"] == pytest.approx(0.006)
    assert attempt["boundaries"]["outer_started_monotonic"] == pytest.approx(110.01)
    assert attempt["boundaries"]["owner"]["capture_started_monotonic"] is not None
    assert attempt["png_sha256"] == hashlib.sha256(b"png-0").hexdigest()
    assert attempt["model_call_counts"]["before"]["started"] == 0
    assert attempt["cleanup"]["active_process_group_count"] == 0
    assert attempt["external_process_cpu_seconds"] is None
    assert attempt["external_process_cpu_reason"] == "external_process_cpu_unavailable"
    assert all(
        stage["wall_seconds"] is None
        and stage["cpu_seconds"] is None
        and stage["reason"] == "no_public_boundary"
        for stage in attempt["private_decoder_substages"].values()
    )


def test_live_config_mismatch_keeps_run_and_all_attempts_ineligible(tmp_path: Path) -> None:
    binding = _live_binding()
    receipt = _live_receipt(1, binding)
    changed_config = dict(binding["config_binding"])
    changed_config["raw_file_sha256"] = hashlib.sha256(b"other-config").hexdigest()
    receipt["config_binding"] = changed_config

    report = _evaluate_live(tmp_path, [receipt], binding)

    run = report["runs"][0]
    assert run["eligible"] is False
    assert "config_binding_mismatch" in run["reason_codes"]
    assert run["attempts_begun"] == 8
    assert len(run["completed_pairs"]) == 8
    assert report["statistics"]["pooled_completed_count"] == 0


def test_live_malformed_bindings_are_refused_without_private_values(tmp_path: Path) -> None:
    binding = _live_binding()
    receipt = _live_receipt(1, binding)
    receipt["producer_binding"] = {
        "binary_sha256": hashlib.sha256(b"producer").hexdigest(),
        "protocol_branch": "ScreenshotArea",
        "producer_path": "/private/PRODUCER_SECRET",
    }
    receipt["config_binding"] = {
        **binding["config_binding"],
        "config_values": {"character": "PRIVATE_CHARACTER_SECRET"},
    }

    report = _evaluate_live(tmp_path, [receipt], binding)
    encoded = json.dumps(report, sort_keys=True)

    assert report["runs"][0]["receipt_refused"] is True
    assert report["runs"][0]["reason_codes"] == ["receipt_schema_invalid"]
    assert "producer_binding" not in report["runs"][0]
    assert "config_binding" not in report["runs"][0]
    assert "/private/PRODUCER_SECRET" not in encoded
    assert "PRIVATE_CHARACTER_SECRET" not in encoded
    assert "producer_path" not in encoded
    assert "config_values" not in encoded


def test_live_model_counter_delta_and_failure_attempt_are_retained(tmp_path: Path) -> None:
    binding = _live_binding()
    receipt = _live_receipt(1, binding, model_delta_at=3)

    report = _evaluate_live(tmp_path, [receipt], binding)

    run = report["runs"][0]
    assert run["receipt_state"] == "failed"
    assert run["attempts_begun"] == 4
    assert len(run["completed_pairs"]) == 3
    assert run["failed_phases_and_reasons"] == [
        {"index": 3, "phase": "model_counter", "reason_code": "model_call_delta_nonzero"}
    ]
    assert run["model_call_deltas"][-1]["delta"]["started"] == 1


def _set_model_counters(attempt: dict[str, object], before: dict[str, int], after: dict[str, int]) -> None:
    owner = attempt["owner"]
    owner["model_calls_before"] = before
    owner["model_calls_after"] = after
    owner["model_call_delta"] = {key: after[key] - before[key] for key in before}


def test_live_impossible_model_counter_triplet_is_refused(tmp_path: Path) -> None:
    binding = _live_binding()
    receipt = _live_receipt(1, binding)
    _set_model_counters(
        receipt["attempts"][0],
        {"started": 0, "succeeded": 0, "failed": 0},
        {"started": 1, "succeeded": 1, "failed": 1},
    )

    report = _evaluate_live(tmp_path, [receipt], binding)

    assert report["runs"][0]["receipt_refused"] is True
    assert report["runs"][0]["reason_codes"] == ["model_call_counters_invalid"]
    assert report["runs"][0]["attempts_begun"] is None


def test_live_decreasing_cumulative_model_counter_is_refused(tmp_path: Path) -> None:
    binding = _live_binding()
    receipt = _live_receipt(1, binding)
    _set_model_counters(
        receipt["attempts"][0],
        {"started": 1, "succeeded": 1, "failed": 0},
        {"started": 1, "succeeded": 1, "failed": 0},
    )
    _set_model_counters(
        receipt["attempts"][1],
        {"started": 0, "succeeded": 0, "failed": 0},
        {"started": 0, "succeeded": 0, "failed": 0},
    )

    report = _evaluate_live(tmp_path, [receipt], binding)

    assert report["runs"][0]["receipt_refused"] is True
    assert report["runs"][0]["reason_codes"] == ["model_call_counters_invalid"]
    assert report["runs"][0]["attempts_begun"] is None


def test_live_inter_sample_model_counter_increase_is_retained_and_ineligible(tmp_path: Path) -> None:
    binding = _live_binding()
    receipt = _live_receipt(1, binding)
    for attempt in receipt["attempts"][1:]:
        _set_model_counters(
            attempt,
            {"started": 1, "succeeded": 1, "failed": 0},
            {"started": 1, "succeeded": 1, "failed": 0},
        )

    report = _evaluate_live(tmp_path, [receipt], binding)

    run = report["runs"][0]
    assert run.get("receipt_refused") is not True
    assert run["eligible"] is False
    assert run["attempts"][1]["model_call_delta"]["started"] == 0
    assert run["attempts"][1]["inter_sample_model_call_delta"] == {
        "started": 1,
        "succeeded": 1,
        "failed": 0,
    }
    assert run["model_call_deltas"][1]["inter_sample_delta"]["succeeded"] == 1
    assert "model_call_delta_nonzero" in run["reason_codes"]
    assert run["observer_source_hashes_before"] == binding["accepted_source_hashes"]
    assert report["statistics"]["pooled_completed_count"] == 0


@pytest.mark.parametrize("provenance_field", ["identity", "generation", "owner_generation", "nonce"])
def test_live_failed_decoded_attempt_requires_unchanged_provenance(
    tmp_path: Path, provenance_field: str
) -> None:
    binding = _live_binding()
    receipt = _live_receipt(1, binding, failed_last=True)
    attempt = receipt["attempts"][-1]
    if provenance_field == "identity":
        attempt["identity"]["sequence"] -= 1
    elif provenance_field == "generation":
        attempt["generation"] = None
    elif provenance_field == "owner_generation":
        attempt["owner"]["generation"] += 1
    else:
        attempt["owner"]["receipt_nonce"] = None

    report = _evaluate_live(tmp_path, [receipt], binding)

    run = report["runs"][0]
    assert run["receipt_refused"] is True
    assert run["reason_codes"] == ["attempt_lifecycle_invalid"]
    assert run["attempts_begun"] is None
    assert run["completed_pairs"] is None


def test_live_decode_exception_keeps_only_partial_diagnostic_interval(tmp_path: Path) -> None:
    binding = _live_binding()
    receipt = _live_receipt(1, binding)
    attempt = receipt["attempts"][0]
    attempt["state"] = "failed"
    attempt["failure"] = _failure("strict_decode", "strict_decode_failed")
    attempt["application_compute_cpu_seconds"] = None
    attempt["application_compute_cpu_method"] = None
    attempt["partial_decode_wall_seconds"] = 0.03
    attempt["partial_decode_cpu_seconds"] = 0.02
    attempt["owner"]["failure"] = None
    receipt["run_state"] = "failed"
    receipt["attempts"] = [attempt]

    report = _evaluate_live(tmp_path, [receipt], binding)

    run = report["runs"][0]
    assert run["attempts_begun"] == 1
    assert run["completed_pairs"] == []
    assert run["attempts"][0]["application_compute_cpu_seconds"] is None
    assert run["attempts"][0]["partial_decode_wall_seconds"] == pytest.approx(0.03)
    assert run["attempts"][0]["partial_decode_cpu_seconds"] == pytest.approx(0.02)


def test_live_early_failure_preserves_null_unreached_boundaries(tmp_path: Path) -> None:
    binding = _live_binding()
    receipt = _live_receipt(1, binding)
    attempt = receipt["attempts"][0]
    attempt.update(
        {
            "state": "failed",
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
            "png_sha256": None,
            "generation": None,
            "identity": None,
            "failure": _failure("status", "status_failed"),
        }
    )
    receipt["run_state"] = "failed"
    receipt["attempts"] = [attempt]

    report = _evaluate_live(tmp_path, [receipt], binding)

    safe = report["runs"][0]["attempts"][0]
    assert safe["boundaries"]["owner"] is None
    assert safe["boundaries"]["png_load_started_monotonic"] is None
    assert safe["intervals"]["owner_ipc_round_trip_wall_seconds"] is None
    assert safe["intervals"]["end_to_end_wall_seconds"] is None
    assert report["runs"][0]["failed_phases_and_reasons"] == [
        {"index": 0, "phase": "status", "reason_code": "status_failed"}
    ]


def test_live_missing_model_counter_stays_null_and_excludes_pair(tmp_path: Path) -> None:
    binding = _live_binding()
    receipt = _live_receipt(1, binding)
    attempt = receipt["attempts"][0]
    attempt["owner"]["model_calls_before"]["started"] = None
    attempt["owner"]["model_calls_after"]["started"] = 1
    attempt["owner"]["model_call_delta"]["started"] = None
    for later_attempt in receipt["attempts"][1:]:
        _set_model_counters(
            later_attempt,
            {"started": 1, "succeeded": 0, "failed": 0},
            {"started": 1, "succeeded": 0, "failed": 0},
        )

    report = _evaluate_live(tmp_path, [receipt], binding)

    run = report["runs"][0]
    assert run["eligible"] is False
    assert run["attempts_begun"] == 8
    assert len(run["completed_pairs"]) == 7
    assert run["attempts"][0]["model_call_delta"]["started"] is None
    assert "model_call_counters_missing" in run["reason_codes"]


def test_live_threshold_equality_and_all_three_decisions(tmp_path: Path) -> None:
    binding = _live_binding()
    prototype = _evaluate_live(
        tmp_path / "prototype",
        [_live_receipt(index, binding, walls=[0.5] * 8, cpus=[0.1] * 8) for index in range(15)],
        binding,
    )
    assert prototype["decision"] == "plan_prototype"
    assert prototype["statistics"]["pooled_application_compute_cpu_p95_seconds"] == pytest.approx(0.1)
    assert prototype["statistics"]["ratio_of_quantiles"] == pytest.approx(0.2)

    defer = _evaluate_live(
        tmp_path / "defer",
        [
            _live_receipt(
                index,
                binding,
                walls=[0.55] * 4 + [0.8] * 4,
                cpus=[0.09] * 4 + [0.08] * 4,
            )
            for index in range(15)
        ],
        binding,
    )
    assert defer["decision"] == "defer_rewrite"
    assert defer["statistics"]["ratio_of_quantiles"] == pytest.approx(0.1125)
    per_sample_cpu_fractions = [
        sample["application_compute_cpu_seconds"] / sample["end_to_end_wall_seconds"]
        for run in defer["runs"]
        for sample in run["completed_pairs"]
    ]
    assert (
        sum(per_sample_cpu_fractions) / len(per_sample_cpu_fractions)
        > defer["statistics"]["ratio_of_quantiles"]
    )

    insufficient = _evaluate_live(
        tmp_path / "insufficient",
        [_live_receipt(index, binding) for index in range(14)],
        binding,
    )
    assert insufficient["decision"] == "insufficient_evidence"
    assert insufficient["statistics"]["eligible_full_eight_sample_run_count"] == 14


def test_live_quantile_uses_nearest_rank_ceiling() -> None:
    assert performance._quantile([float(value) for value in range(1, 21)], 0.95) == 19.0


def test_live_repeatability_requires_eighty_percent_full_runs(tmp_path: Path) -> None:
    binding = _live_binding()
    twelve_of_fifteen = [
        _live_receipt(index, binding, walls=[0.5] * 8, cpus=[0.11 if index < 12 else 0.04] * 8)
        for index in range(15)
    ]
    at_eighty = _evaluate_live(tmp_path / "at-eighty", twelve_of_fifteen, binding)
    assert at_eighty["decision"] == "plan_prototype"
    assert at_eighty["statistics"]["run_trigger_repeatability"]["cpu_trigger_runs"] == 12

    eleven_of_fifteen = [
        _live_receipt(index, binding, walls=[0.5] * 8, cpus=[0.11 if index < 11 else 0.04] * 8)
        for index in range(15)
    ]
    below_eighty = _evaluate_live(tmp_path / "below-eighty", eleven_of_fifteen, binding)
    assert below_eighty["decision"] == "defer_rewrite"
    assert below_eighty["statistics"]["run_trigger_repeatability"]["cpu_trigger_runs"] == 11
    assert "migration_trigger_not_repeatable" in below_eighty["reason_codes"]


def test_live_119_pairs_fails_but_120_pairs_across_fifteen_runs_qualifies(tmp_path: Path) -> None:
    binding = _live_binding()
    one_short_values = [_live_receipt(index, binding) for index in range(14)]
    one_short = _live_receipt(14, binding, failed_last=True)
    last_attempt = one_short["attempts"][-1]
    last_attempt["failure"] = _failure("strict_decode", "strict_decode_failed")
    last_attempt["application_compute_cpu_seconds"] = None
    last_attempt["application_compute_cpu_method"] = None
    last_attempt["partial_decode_wall_seconds"] = 0.03
    last_attempt["partial_decode_cpu_seconds"] = 0.02
    one_short_values.append(one_short)
    report_119 = _evaluate_live(tmp_path / "119", one_short_values, binding)
    assert report_119["statistics"]["pooled_completed_count"] == 119
    assert report_119["decision"] == "insufficient_evidence"

    report_120 = _evaluate_live(
        tmp_path / "120", [_live_receipt(index, binding) for index in range(15)], binding
    )
    assert report_120["statistics"]["pooled_completed_count"] == 120
    assert report_120["decision"] == "defer_rewrite"


def test_live_refused_receipt_poisoning_is_not_ignored_in_full_collection(tmp_path: Path) -> None:
    binding = _live_binding()
    valid = [_live_receipt(index, binding) for index in range(15)]
    malformed = _live_receipt(99, binding)
    malformed["attempts"][0]["unrecognized_stage"] = True

    report = _evaluate_live(tmp_path, [*valid, malformed], binding)

    refused = report["runs"][-1]
    assert report["statistics"]["pooled_completed_count"] == 120
    assert report["decision"] == "insufficient_evidence"
    assert "receipt_schema_invalid" in report["reason_codes"]
    assert "unknown_required_stage" in report["reason_codes"]
    assert refused["receipt_sha256"] is not None
    assert refused["attempts_begun"] is None
    assert refused["attempts"] is None
    assert refused["failed_phases_and_reasons"] is None
    assert refused["model_call_deltas"] is None
    assert refused["interval_availability"] is None


def test_live_counterfactual_deadline_requires_three_distinct_runs_and_stages(tmp_path: Path) -> None:
    binding = _live_binding()
    values: list[dict[str, object]] = []
    for index in range(18):
        if index < 3:
            receipt = _live_receipt(
                index, binding, walls=[0.5] * 7 + [1.05], cpus=[0.04] * 7 + [0.1], failed_last=True
            )
        else:
            receipt = _live_receipt(index, binding)
        values.append(receipt)
    report = _evaluate_live(tmp_path, values, binding)
    assert report["decision"] == "plan_prototype"
    assert report["statistics"]["deadline_failure_trigger"] is True
    assert len(report["statistics"]["deadline_failure_candidates"]) == 3
    assert len({item["receipt_sha256"] for item in report["statistics"]["deadline_failure_candidates"]}) == 3

    incomplete = []
    for index in range(18):
        if index < 3:
            receipt = _live_receipt(
                index,
                binding,
                walls=[0.5] * 7 + [1.05],
                cpus=[0.04] * 7 + [0.1],
                failed_last=True,
            )
            receipt["attempts"][-1]["png_load_finished_monotonic"] = None
        else:
            receipt = _live_receipt(index, binding)
        incomplete.append(receipt)
    no_candidate = _evaluate_live(tmp_path / "missing-stage", incomplete, binding)
    assert no_candidate["statistics"]["deadline_failure_trigger"] is False
    assert no_candidate["statistics"]["deadline_failure_candidates"] == []


def test_live_duplicate_digest_and_run_id_are_not_eligible(tmp_path: Path) -> None:
    binding = _live_binding()
    receipt = _live_receipt(1, binding)
    report = _evaluate_live(tmp_path, [receipt, receipt], binding)
    assert len(report["runs"]) == 2
    assert all(run["eligible"] is False for run in report["runs"])
    assert "duplicate_receipt_digest" in report["runs"][0]["reason_codes"]
    assert "duplicate_run_id" in report["runs"][0]["reason_codes"]

    one = _live_receipt(2, binding)
    two = _live_receipt(3, binding)
    two["run_id"] = one["run_id"]
    duplicate_id = _evaluate_live(tmp_path / "ids", [one, two], binding)
    assert all(run["eligible"] is False for run in duplicate_id["runs"])
    assert "duplicate_run_id" in duplicate_id["runs"][0]["reason_codes"]


def test_live_bad_hash_duplicate_json_nonfinite_and_depth_are_refused(tmp_path: Path) -> None:
    binding = _live_binding()
    binding_path = tmp_path / "binding.json"
    digest = _write_binding(binding_path, binding)
    good = tmp_path / "good.json"
    _write_live_receipt(good, _live_receipt(1, binding))
    with pytest.raises(PerformanceInputError, match="binding_digest_mismatch"):
        build_live_report([good], accepted_binding=binding_path, binding_sha256="0" * 64)

    bad_values = [
        b'{"schema":"x","schema":"y"}',
        b'{"schema":"x","clock":NaN}',
        b"[" * 33 + b"]" * 33,
    ]
    reports = []
    for index, raw in enumerate(bad_values):
        path = tmp_path / f"bad-{index}.json"
        path.write_bytes(raw)
        reports.append(build_live_report([path], accepted_binding=binding_path, binding_sha256=digest))
    assert reports[0]["runs"][0]["reason_codes"] == ["receipt_duplicate_or_non_string_key"]
    assert reports[1]["runs"][0]["reason_codes"] == ["receipt_invalid_json"]
    assert reports[2]["runs"][0]["reason_codes"] == ["receipt_json_nesting_exceeded"]

    wrong_type = _live_receipt(8, binding)
    wrong_type["attempts"][0]["state"] = []
    typed_report = _evaluate_live(tmp_path / "wrong-type", [wrong_type], binding)
    assert typed_report["runs"][0]["reason_codes"] == ["attempt_timing_invalid"]

    wrong_version = _live_receipt(9, binding)
    wrong_version["schema_version"] = True
    version_report = _evaluate_live(tmp_path / "wrong-version", [wrong_version], binding)
    assert version_report["runs"][0]["reason_codes"] == ["receipt_schema_invalid"]


def test_live_source_and_producer_mismatches_retain_validly_parsed_attempts(tmp_path: Path) -> None:
    binding = _live_binding()
    source_mismatch = _live_receipt(1, binding)
    source_mismatch["observer_source_hashes_after"] = dict(binding["accepted_source_hashes"])
    source_mismatch["observer_source_hashes_after"][sorted(performance.LIVE_SOURCE_FILES)[0]] = (
        hashlib.sha256(b"changed daemon").hexdigest()
    )
    producer_mismatch = _live_receipt(2, binding)
    producer_mismatch["producer_binding"] = {
        "binary_sha256": hashlib.sha256(b"other binary").hexdigest(),
        "protocol_branch": "ScreenshotArea",
    }
    report = _evaluate_live(tmp_path, [source_mismatch, producer_mismatch], binding)
    assert report["runs"][0]["eligible"] is False
    assert report["runs"][0]["attempts_begun"] == 8
    assert "source_binding_drift" in report["runs"][0]["reason_codes"]
    assert report["runs"][1]["eligible"] is False
    assert report["runs"][1]["attempts_begun"] == 8
    assert "producer_binding_mismatch" in report["runs"][1]["reason_codes"]

    values = [_live_receipt(index, binding) for index in range(15)]
    global_source_mismatch = _live_receipt(99, binding)
    global_source_mismatch["observer_source_hashes_after"] = dict(binding["accepted_source_hashes"])
    global_source_mismatch["observer_source_hashes_after"][sorted(performance.LIVE_SOURCE_FILES)[0]] = (
        hashlib.sha256(b"changed daemon").hexdigest()
    )
    values.append(global_source_mismatch)
    globally_blocked = _evaluate_live(tmp_path / "global-mismatch", values, binding)
    assert globally_blocked["statistics"]["eligible_full_eight_sample_run_count"] == 15
    assert globally_blocked["decision"] == "insufficient_evidence"
    assert globally_blocked["statistics"]["binding_mismatch_run_count"] == 1


def test_live_receipt_size_and_symlink_are_refused_without_path_output(tmp_path: Path) -> None:
    binding = _live_binding()
    binding_path = tmp_path / "binding.json"
    digest = _write_binding(binding_path, binding)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (MAX_LIVE_RECEIPT_BYTES + 1))
    target = tmp_path / "target.json"
    _write_live_receipt(target, _live_receipt(1, binding))
    link = tmp_path / "private-link.json"
    link.symlink_to(target)

    report = build_live_report([oversized, link], accepted_binding=binding_path, binding_sha256=digest)
    assert [run["reason_codes"] for run in report["runs"]] == [
        ["receipt_size_out_of_bounds"],
        ["receipt_unreadable"],
    ]
    assert "private-link.json" not in json.dumps(report)


def test_live_descriptor_growth_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _live_binding()
    binding_path = tmp_path / "binding.json"
    digest = _write_binding(binding_path, binding)
    path = tmp_path / "run.json"
    _write_live_receipt(path, _live_receipt(1, binding))
    original_read = performance.os.read
    changed = False

    def grow_descriptor(fd: int, size: int) -> bytes:
        nonlocal changed
        chunk = original_read(fd, min(size, 32))
        if not changed and os.fstat(fd).st_ino == path.stat().st_ino:
            changed = True
            path.write_bytes(path.read_bytes() + b" ")
        return chunk

    monkeypatch.setattr(performance.os, "read", grow_descriptor)
    report = build_live_report([path], accepted_binding=binding_path, binding_sha256=digest)
    assert report["runs"][0]["reason_codes"] == ["receipt_changed_during_read"]


def test_live_binding_descriptor_drift_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binding = _live_binding()
    binding_path = tmp_path / "binding.json"
    digest = _write_binding(binding_path, binding)
    path = tmp_path / "run.json"
    _write_live_receipt(path, _live_receipt(1, binding))
    original_read = performance.os.read
    changed = False

    def mutate_binding(fd: int, size: int) -> bytes:
        nonlocal changed
        chunk = original_read(fd, min(size, 32))
        if not changed and os.fstat(fd).st_ino == binding_path.stat().st_ino:
            changed = True
            binding_path.write_bytes(binding_path.read_bytes() + b" ")
        return chunk

    monkeypatch.setattr(performance.os, "read", mutate_binding)
    with pytest.raises(PerformanceInputError, match="binding_changed_during_read"):
        build_live_report([path], accepted_binding=binding_path, binding_sha256=digest)


def test_live_missing_unknown_stage_and_late_decode_pair_are_retained(tmp_path: Path) -> None:
    binding = _live_binding()
    missing = _live_receipt(1, binding)
    del missing["attempts"][0]["owner_ipc_returned_monotonic"]
    unknown = _live_receipt(2, binding)
    unknown["attempts"][0]["child_cpu_seconds"] = 0.01
    late = _live_receipt(3, binding)
    late_attempt = late["attempts"][0]
    late_attempt["state"] = "failed"
    late_attempt["failure"] = _failure("deadline", "sample_deadline_missed")
    late_attempt["strict_decode_finished_monotonic"] = late_attempt["deadline_monotonic"] + 0.01
    late_attempt["sample_completed_monotonic"] = late_attempt["deadline_monotonic"] + 0.02
    late["run_state"] = "failed"
    late["attempts"] = [late_attempt]
    report = _evaluate_live(tmp_path, [missing, unknown, late], binding)

    assert report["runs"][0]["reason_codes"] == ["missing_required_stage"]
    assert report["runs"][1]["reason_codes"] == ["unknown_required_stage"]
    assert report["runs"][1]["receipt_refused"] is True
    assert report["runs"][1]["attempts_begun"] is None
    assert report["runs"][1]["attempts"] is None
    assert report["runs"][1]["interval_availability"] is None
    assert report["runs"][2]["eligible"] is True
    assert report["runs"][2]["completed_pairs"] == [
        {
            "index": 0,
            "end_to_end_wall_seconds": pytest.approx(1.0),
            "application_compute_cpu_seconds": pytest.approx(0.04),
        }
    ]
    assert report["runs"][2]["attempts"][0]["paired_completed"] is True
    assert report["runs"][2]["attempts"][0]["timing_qualified"] is False
    assert "sample_deadline_missed" in report["runs"][2]["reason_codes"]


@pytest.mark.parametrize(
    "boundary",
    [
        "outer_started_monotonic",
        "status_returned_monotonic",
        "owner_ipc_request_started_monotonic",
        "owner_ipc_returned_monotonic",
        "png_publication_end_monotonic",
        "png_load_started_monotonic",
        "png_load_finished_monotonic",
        "strict_decode_started_monotonic",
        "strict_decode_finished_monotonic",
        "owner.owner_entered_monotonic",
        "owner.request_started_monotonic",
        "owner.lock_acquired_monotonic",
        "owner.ack_sent_monotonic",
        "owner.capture_started_monotonic",
        "owner.capture_returned_monotonic",
        "owner.response_ready_monotonic",
        "owner.generation",
    ],
)
def test_live_late_decode_requires_all_causal_boundaries_and_owner_generation(
    tmp_path: Path, boundary: str
) -> None:
    binding = _live_binding()
    malformed = _live_receipt(99, binding, failed_last=True)
    attempt = malformed["attempts"][-1]
    if boundary.startswith("owner."):
        attempt["owner"][boundary.removeprefix("owner.")] = None
    else:
        attempt[boundary] = None
    receipts = [_live_receipt(index, binding) for index in range(15)] + [malformed]

    report = _evaluate_live(tmp_path, receipts, binding)

    refused = report["runs"][-1]
    assert refused["receipt_refused"] is True
    assert refused["reason_codes"] == ["missing_required_stage"]
    assert refused["completed_pairs"] is None
    assert report["statistics"]["pooled_completed_count"] == 120
    assert report["statistics"]["eligible_full_eight_sample_run_count"] == 15
    assert report["decision"] == "insufficient_evidence"


def test_live_interrupted_after_decode_retains_pair_without_sample_completion(
    tmp_path: Path,
) -> None:
    binding = _live_binding()
    receipt = _live_receipt(100, binding)
    receipt["run_state"] = "incomplete"
    attempt = receipt["attempts"][0]
    attempt["state"] = "incomplete"
    attempt["sample_completed_monotonic"] = None
    receipt["attempts"] = [attempt]

    report = _evaluate_live(tmp_path, [receipt], binding)

    run = report["runs"][0]
    assert run.get("receipt_refused", False) is False
    assert run["eligible"] is True
    assert run["attempts"][0]["boundaries"]["strict_decode_finished_monotonic"] is not None
    assert run["attempts"][0]["boundaries"]["sample_completed_monotonic"] is None
    assert run["attempts"][0]["paired_completed"] is True
    assert run["attempts"][0]["timing_qualified"] is False
    assert run["completed_pairs"] == [
        {
            "index": 0,
            "end_to_end_wall_seconds": pytest.approx(0.5),
            "application_compute_cpu_seconds": pytest.approx(0.04),
        }
    ]


def test_live_target_cadence_bounds_and_exact_sample_deadline(tmp_path: Path) -> None:
    binding = _live_binding()
    lower = _live_receipt(1, binding)
    _shift_attempt_timestamps(lower["attempts"][0], 0.25)
    upper = _live_receipt(2, binding)
    _shift_attempt_timestamps(upper["attempts"][1], 0.25)
    too_fast = _live_receipt(3, binding)
    _shift_attempt_timestamps(too_fast["attempts"][0], 0.26)
    bad_targets = _live_receipt(4, binding, interval_seconds=0.75)
    exact_deadline = _live_receipt(5, binding)
    for exact_attempt in exact_deadline["attempts"]:
        exact_attempt["strict_decode_finished_monotonic"] = exact_attempt["deadline_monotonic"] - 0.001
        exact_attempt["sample_completed_monotonic"] = exact_attempt["deadline_monotonic"]
    failed_cadence = _live_receipt(6, binding, failed_last=True)
    _shift_attempt_timestamps(failed_cadence["attempts"][-1], 0.26)
    report = _evaluate_live(
        tmp_path, [lower, upper, too_fast, bad_targets, exact_deadline, failed_cadence], binding
    )

    assert report["runs"][0]["attempts"][0]["paired_completed"] is True
    assert report["runs"][0]["attempts"][0]["intervals"]["end_to_end_wall_seconds"] == pytest.approx(0.5)
    assert report["runs"][0]["timing_qualified"] is True
    assert report["runs"][0]["timing_qualification_violations"] == []
    assert report["runs"][1]["timing_qualified"] is True
    assert report["runs"][1]["timing_qualification_violations"] == []
    assert report["runs"][2]["eligible"] is False
    assert report["runs"][2]["timing_qualification_violations"][0]["kind"] == "actual_start_cadence"
    assert report["runs"][3]["receipt_refused"] is True
    assert report["runs"][3]["reason_codes"] == ["run_timing_invalid"]
    assert report["runs"][4]["attempts"][0]["intervals"]["end_to_end_wall_seconds"] == pytest.approx(0.989)
    assert report["runs"][4]["timing_qualified"] is True
    assert report["runs"][5]["eligible"] is True
    assert report["runs"][5]["timing_qualified"] is False
    assert report["runs"][5]["timing_qualification_violations"][0]["kind"] == "actual_start_cadence"


def test_live_failed_owner_capture_does_not_create_a_decoded_pair(tmp_path: Path) -> None:
    binding = _live_binding()
    receipt = _live_receipt(1, binding, failed_last=True)
    receipt["attempts"][-1]["owner"]["failure"] = _failure("owner_capture", "owner_capture_failed")

    report = _evaluate_live(tmp_path, [receipt], binding)

    run = report["runs"][0]
    assert run["eligible"] is False
    assert len(run["completed_pairs"]) == 7
    assert run["attempts"][-1]["paired_completed"] is False
    assert "owner_capture_failed" in run["attempts"][-1]["pair_provenance_reason_codes"]


def test_live_cleanup_failure_after_decode_is_retained_without_pairing(tmp_path: Path) -> None:
    binding = _live_binding()
    receipt = _live_receipt(1, binding, failed_last=True)
    last = receipt["attempts"][-1]
    last["failure"] = _failure("cleanup", "cleanup_failed")
    last["owner"]["failure"] = _failure("cleanup", "cleanup_failed")
    last["owner"]["cleanup"]["capture_process_reaped"] = False

    report = _evaluate_live(tmp_path, [receipt], binding)

    run = report["runs"][0]
    attempt = run["attempts"][-1]
    assert run.get("receipt_refused") is not True
    assert run["eligible"] is False
    assert len(run["completed_pairs"]) == 7
    assert attempt["application_compute_cpu_seconds"] == pytest.approx(0.04)
    assert attempt["cleanup"]["capture_process_reaped"] is False
    assert attempt["cleanup_ok"] is False
    assert attempt["paired_completed"] is False
    assert attempt["failure"] == _failure("cleanup", "cleanup_failed")


def test_live_cli_is_explicitly_offline_and_requires_parent_binding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binding = _live_binding()
    binding_path = tmp_path / "binding.json"
    digest = _write_binding(binding_path, binding)
    receipt_path = tmp_path / "run.json"
    _write_live_receipt(receipt_path, _live_receipt(1, binding))
    assert performance.main(["--live-receipt", str(receipt_path)]) == 2
    assert "requires --accepted-binding" in capsys.readouterr().err

    assert (
        performance.main(
            [
                "--live-receipt",
                str(receipt_path),
                "--accepted-binding",
                str(binding_path),
                "--binding-sha256",
                digest,
            ]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output["decision"] == "insufficient_evidence"
    assert "not independently authenticated" in output["limitations"][1]


def test_live_receipt_count_is_bounded_before_evaluation(tmp_path: Path) -> None:
    binding = _live_binding()
    binding_path = tmp_path / "binding.json"
    digest = _write_binding(binding_path, binding)
    with pytest.raises(PerformanceInputError, match="live receipts"):
        build_live_report(
            [tmp_path / f"r-{index}" for index in range(65)],
            accepted_binding=binding_path,
            binding_sha256=digest,
        )
