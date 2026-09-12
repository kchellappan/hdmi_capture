#!/usr/bin/env python3
"""Drive the capture loop against `vivid`, the kernel's virtual video driver.

tests/fake.py covers everything downstream of a Frame, but it cannot reach the parts that
only a real V4L2 device exercises: the ioctl path, buffer mmap, QBUF/DQBUF, and the
timestamp the kernel attaches. vivid provides those without hardware, which makes this the
only way to test the capture loop in CI.

What it is not: this capture card. vivid reproduces none of the firmware behaviour in
docs/hardware.md -- it produces clean frames, on time, forever. A pass here says the V4L2
plumbing is correct, not that capture works.

Skips rather than fails when vivid is not loaded, since it is not available everywhere.
"""
from __future__ import annotations

import sys

import fake  # noqa: F401  - puts the repo root on sys.path

from vcap.device import enumerate_devices
from vcap.source import NoSignal, StreamError, VideoSource

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")


def truthy(name: str, got) -> None:
    if not got:
        FAILURES.append(f"{name}: got {got!r}, want something truthy")


def find_vivid():
    for dev in enumerate_devices():
        if "vivid" in dev.driver.lower() or "vivid" in dev.card.lower():
            if dev.modes:
                return dev
    return None


def main() -> int:
    device = find_vivid()
    if device is None:
        print("  skip  vivid is not loaded; the capture loop is untested here")
        return 0

    mode = next((m for m in device.modes if m.pixelformat == "YUYV"), device.modes[0])
    print(f"  using {device.path}: {mode}")

    try:
        with VideoSource(device, mode.pixelformat, mode.width, mode.height,
                         fps=30, buffers=4) as src:
            negotiated = src.negotiated
            check("format was granted as requested",
                  (negotiated.pixelformat, negotiated.width, negotiated.height),
                  (mode.pixelformat, mode.width, mode.height))
            truthy("sizeimage is set", negotiated.sizeimage > 0)

            frames = []
            for i, frame in enumerate(src.frames(timeout=5.0)):
                frames.append(frame)
                if i >= 19:
                    break

            check("twenty frames arrived", len(frames), 20)
            truthy("frames have payload", all(len(f) > 0 for f in frames))

            # The point of the whole exercise: the kernel attached a usable timestamp.
            truthy("timestamps are non-zero", all(f.ts_mono_ns > 0 for f in frames))
            timestamps = [f.ts_mono_ns for f in frames]
            check("timestamps increase monotonically",
                  timestamps == sorted(timestamps), True)
            truthy("timestamps advance between frames",
                   timestamps[-1] > timestamps[0])

            # Sequence numbers must be contiguous on an idle machine; a jump here would
            # mean the loop is not requeueing buffers fast enough.
            sequences = [f.seq for f in frames]
            check("sequence numbers are contiguous",
                  sequences, list(range(sequences[0], sequences[0] + 20)))
            check("no frames were dropped", src.driver_dropped, 0)

            # Buffers must be returned to the driver. With four buffers, capturing
            # twenty frames is only possible if each one was requeued.
            truthy("buffers are being recycled", len(frames) > src.n_buffers)

            stats = src.stats()
            check("stats count what was yielded", stats["frames"], 20)
            # The absence of a "card dropped" figure is deliberate; see CLAUDE.md.
            check("stats claim nothing unverifiable",
                  sorted(stats), ["corrupt", "driver_dropped", "driver_errors",
                                  "frames"])
    except (NoSignal, StreamError, OSError) as exc:
        FAILURES.append(f"streaming from vivid failed: {type(exc).__name__}: {exc}")

    for line in FAILURES:
        print(f"  FAIL  {line}")
    if not FAILURES:
        print("  ok    capture loop against vivid: ioctls, mmap, timestamps, requeue")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
