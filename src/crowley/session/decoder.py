"""Decode a calibrated lossless 2x2 status crop into the strict frame protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .protocol import Frame, ProtocolContract, ProtocolError, decode_frame, unpack_cells, unsigned_delta


@dataclass(frozen=True)
class Sample:
    kind: str
    frame: Frame | None
    capture_monotonic: float
    crop: tuple[int, int, int, int]
    crc32: int | None
    window_identity: str


class PixelDecoder:
    def __init__(
        self,
        *,
        crop: tuple[int, int, int, int] = (80, 48, 256, 256),
        threshold: int = 128,
        contract: ProtocolContract | None = None,
    ):
        self.crop = crop
        self.threshold = threshold
        self.contract = contract
        self.boot: str | None = None
        self.sequence: int | None = None
        self.uptime_ms: int | None = None
        self.crc32: int | None = None
        self.window_identity: str | None = None
        self._boot_change_allowed = False

    def allow_boot_change_once(self) -> None:
        """Arm the one fresh boot transition caused by a verified reload."""

        self._boot_change_allowed = True

    def decode(self, pixels: Any, *, monotonic: float, window_identity: str = "") -> Sample:
        try:
            width, height = pixels.size
            if (width, height) != (256, 256):
                raise ProtocolError("calibrated status crop must be 256x256")
            rows: list[list[int]] = []
            for y in range(128):
                row: list[int] = []
                for x in range(128):
                    cell: list[int] = []
                    for dy in range(2):
                        for dx in range(2):
                            red, green, blue, *_ = pixels.getpixel((x * 2 + dx, y * 2 + dy))
                            luminance = (299 * red + 587 * green + 114 * blue) // 1000
                            if luminance not in (0, 255):
                                raise ProtocolError("status cell pixel is neither black nor white")
                            cell.append(int(luminance >= self.threshold))
                    if len(set(cell)) != 1:
                        raise ProtocolError("status cell physical pixels disagree")
                    row.append(cell[0])
                rows.append(row)
            frame = decode_frame(unpack_cells(rows), contract=self.contract)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProtocolError(f"invalid pixel crop: {exc}") from exc
        if window_identity == "":
            raise ProtocolError("window identity is required")
        if self.window_identity is not None and window_identity != self.window_identity:
            raise ProtocolError("status window identity changed")
        self.window_identity = window_identity
        if self.boot is not None and frame.boot != self.boot:
            if not self._boot_change_allowed:
                raise ProtocolError("unexpected status boot change")
            self._boot_change_allowed = False
            self.sequence = None
            self.uptime_ms = None
            self.crc32 = None
        if self.boot == frame.boot and self.sequence == frame.sequence and self.uptime_ms == frame.uptime_ms:
            if self.crc32 != frame.crc32:
                raise ProtocolError("duplicate frame tuple has a different CRC")
            return Sample("NO_CHANGE", frame, monotonic, self.crop, frame.crc32, window_identity)
        if self.boot == frame.boot and self.sequence is not None:
            if frame.sequence <= self.sequence:
                raise ProtocolError("status sequence is stale")
            if self.uptime_ms is not None:
                unsigned_delta(frame.uptime_ms, self.uptime_ms)
        self.boot, self.sequence, self.uptime_ms, self.crc32 = (
            frame.boot,
            frame.sequence,
            frame.uptime_ms,
            frame.crc32,
        )
        return Sample("FRESH", frame, monotonic, self.crop, frame.crc32, window_identity)


__all__ = ["PixelDecoder", "Sample"]
