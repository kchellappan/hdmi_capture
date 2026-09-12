"""Measuring what a device can actually do, rather than reading what it claims.

A UVC device's descriptors are a list of modes the firmware is willing to accept, not a
promise that it can sustain them. On this project's card every mode is advertised for
both MJPEG and YUYV with identical frame rates, which cannot all be true: 1920x1080 at
60 Hz in YUYV is 249 MB/s, and the same geometry in MJPEG is around 12 MB/s. The
descriptors are the same either way.

So the only trustworthy capability report is an empirical one: negotiate the mode, stream
for a fixed interval, and count what arrives. That is slow -- a second or two per mode --
which is why it is a separate step from enumeration rather than folded into it.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from .source import NoSignal, StreamError, VideoSource


@dataclass
class Measurement:
    """What a mode did when it was actually tried."""
    pixelformat: str
    width: int
    height: int
    fps_requested: float | None
    fps_granted: float | None = None
    fps_measured: float | None = None
    frames: int = 0
    driver_dropped: int = 0
    driver_errors: int = 0
    corrupt: int = 0
    interval_ms: dict = field(default_factory=dict)
    bytes_per_frame: dict = field(default_factory=dict)
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.error is None and self.frames > 1

    def summary(self) -> str:
        if self.error:
            return (f"{self.pixelformat} {self.width}x{self.height} "
                    f"@{self.fps_requested or '?'}: {self.error}")
        rate = f"{self.fps_measured:.1f}" if self.fps_measured else "?"
        claimed = f"{self.fps_requested:g}" if self.fps_requested else "?"
        note = ""
        if self.fps_measured and self.fps_requested:
            ratio = self.fps_measured / self.fps_requested
            if ratio < 0.9:
                note = f"  <- claims {claimed}, delivers {rate}"
        mb = self.bytes_per_frame.get("mean", 0) / 1024
        return (f"{self.pixelformat} {self.width}x{self.height} "
                f"@{claimed} -> {rate} fps  {mb:.0f} KB/frame"
                f"  dropped={self.driver_dropped} corrupt={self.corrupt}{note}")


def measure(device, pixelformat: str, width: int, height: int,
            fps: float | None = None, *, seconds: float = 2.0,
            timeout: float = 3.0) -> Measurement:
    """Stream one mode for `seconds` and report what actually happened.

    `seconds` defaults to 2 because a shorter window cannot distinguish 30 from 60 Hz
    reliably once a single stall is averaged in, and a longer one makes probing every
    mode of a card take minutes.
    """
    result = Measurement(pixelformat, width, height, fps)
    try:
        with VideoSource(device, pixelformat, width, height, fps=fps) as src:
            granted = src.negotiated
            result.fps_granted = granted.fps_granted
            # The driver may have granted different geometry than requested. Report what
            # was measured against what is real, not against what was asked for.
            result.width, result.height = granted.width, granted.height
            result.pixelformat = granted.pixelformat

            timestamps: list[int] = []
            sizes: list[int] = []
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    frame = src.read(timeout=timeout)
                except NoSignal as exc:
                    if not timestamps:
                        result.error = f"no frames: {exc}"
                        return result
                    break
                timestamps.append(frame.ts_mono_ns)
                sizes.append(len(frame))

            result.frames = len(timestamps)
            result.driver_dropped = src.driver_dropped
            result.driver_errors = src.driver_errors
            result.corrupt = src.corrupt

            if len(timestamps) > 1:
                span_s = (timestamps[-1] - timestamps[0]) / 1e9
                if span_s > 0:
                    result.fps_measured = (len(timestamps) - 1) / span_s
                deltas = [(b - a) / 1e6 for a, b in zip(timestamps, timestamps[1:])]
                result.interval_ms = {
                    "mean": statistics.mean(deltas),
                    "median": statistics.median(deltas),
                    "min": min(deltas),
                    "max": max(deltas),
                    # Jitter matters more than mean rate for alignment: a steady 30 Hz
                    # pairs with controls better than a 60 Hz average that arrives in
                    # bursts.
                    "stdev": statistics.stdev(deltas) if len(deltas) > 2 else 0.0,
                }
            if sizes:
                result.bytes_per_frame = {
                    "mean": statistics.mean(sizes), "min": min(sizes), "max": max(sizes)}
    except (OSError, StreamError) as exc:
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def measure_all(device, *, pixelformats: tuple[str, ...] = ("MJPG",),
                seconds: float = 2.0, max_rate_only: bool = True) -> list[Measurement]:
    """Measure every advertised mode in the given pixel formats.

    By default only the highest advertised rate for each geometry is tried, because that
    is the claim most likely to be false and the lower rates are usually derived from it.
    """
    out: list[Measurement] = []
    for mode in device.modes:
        if mode.pixelformat not in pixelformats:
            continue
        rates = mode.rates_hz or (None,)
        chosen = (max(rates),) if max_rate_only and mode.rates_hz else rates
        for rate in chosen:
            out.append(measure(device, mode.pixelformat, mode.width, mode.height,
                               rate, seconds=seconds))
    return out
