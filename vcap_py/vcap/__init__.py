"""vcap -- HDMI capture for control-and-video data collection on Linux.

Built around one property: every frame carries the kernel's CLOCK_MONOTONIC timestamp,
untouched, from the capture buffer through to the on-disk index. That is what makes a
recording alignable against controls captured on the same machine, and everything else
here is arranged to avoid damaging it.

    from vcap import find_capture_card, Session

    device = find_capture_card("MACROSILICON")
    with Session("data/run-04", device, fps=60) as s:
        s.record(seconds=30)

Reading one back:

    from vcap import Recording

    with Recording("data/run-04") as rec:
        frame = rec.at(control_sample_ts_ns, tolerance_ns=8_000_000)

The core imports only the standard library. `vcap.decode` and `vcap.export` are the one
declared exception; see decode.py for where that line is and why.
"""
from __future__ import annotations

from .device import Device, Mode, enumerate_devices, find_capture_card, open_device
from .frame import (FLAG_CORRUPT, FLAG_DRIVER_ERROR, FLAG_GAP_BEFORE, Frame,
                    jpeg_dimensions, looks_like_jpeg)
from .index import Entry, IndexReader
from .reader import Recording
from .ring import LatestFrame
from .session import Session
from .source import Negotiated, NoSignal, StreamError, VideoSource
from .writer import AsyncWriter, SegmentWriter, WriterOverrun

__version__ = "0.1.0"

__all__ = [
    "Device", "Mode", "enumerate_devices", "find_capture_card", "open_device",
    "Frame", "jpeg_dimensions", "looks_like_jpeg",
    "FLAG_CORRUPT", "FLAG_DRIVER_ERROR", "FLAG_GAP_BEFORE",
    "Entry", "IndexReader", "Recording", "LatestFrame", "Session",
    "Negotiated", "NoSignal", "StreamError", "VideoSource",
    "AsyncWriter", "SegmentWriter", "WriterOverrun",
    "__version__",
]
