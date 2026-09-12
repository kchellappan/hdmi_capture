"""Recording a session: source, writer and manifest, wired together.

This is the layer tools/vcap-record is a thin shell around, and the one a parent repo
calls when it wants video for an episode. It owns exactly one policy decision -- that a
recorder never drops a frame on purpose -- and defers everything else.

What it deliberately does not own is the word "episode". A parent repo that submodules
this alongside rpi_gamepad_bridge has both halves of a demonstration and is the only
place that knows where one begins and ends, what the task was, and whether it succeeded.
If that concept lived here it would have to be kept in step with a control library this
repo does not depend on and cannot see. See docs/composition.md.
"""
from __future__ import annotations

import os
import time

from . import manifest as manifest_mod
from .source import NoSignal, StreamError, VideoSource
from .writer import AsyncWriter, SegmentWriter


class Session:
    """One recording, from STREAMON to a finalized manifest.

        with Session("data/run-04", device, fps=60) as s:
            s.record(seconds=30)

    Or drive the loop yourself, which is what a parent repo wants so that it can stop on
    its own signal rather than a duration:

        with Session("data/run-04", device, fps=60) as s:
            for frame in s.frames():
                if done: break
    """

    def __init__(self, directory: str, device, *, pixelformat: str = "MJPG",
                 width: int = 1920, height: int = 1080, fps: float | None = 60,
                 name: str = "session", buffers: int = 4, queue_depth: int = 240,
                 segment_bytes: int | None = None, fsync_every: int = 0,
                 patience: float | None = 5.0, notes: dict | None = None):
        self.directory = directory
        self.device = device
        self.name = name
        self.patience = patience
        self.notes = notes or {}
        self._requested = {
            "pixelformat": pixelformat, "width": width, "height": height, "fps": fps}

        self.source = VideoSource(device, pixelformat, width, height, fps=fps,
                                  buffers=buffers)
        self._writer: SegmentWriter | None = None
        self._async: AsyncWriter | None = None
        self._segment_bytes = segment_bytes
        self._fsync_every = fsync_every
        self._queue_depth = queue_depth
        self.manifest: dict | None = None
        self.manifest_path = os.path.join(directory, "manifest.json")

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> "Session":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self):
        negotiated = self.source.open()
        self._writer = SegmentWriter(self.directory, self.name,
                                     segment_bytes=self._segment_bytes,
                                     fsync_every=self._fsync_every)
        self._async = AsyncWriter(self._writer, depth=self._queue_depth)
        self.manifest = manifest_mod.build(
            session=self.name, device=self.device, negotiated=negotiated,
            requested=self._requested,
            stream_file=self._writer.segments[0]["stream"],
            index_file=self._writer.segments[0]["index"],
            notes=self.notes)
        self.manifest["segments"] = self._writer.segments
        # Written before the first frame so that a session killed by a crash or a power
        # loss still describes itself. It is rewritten on close with the counters.
        manifest_mod.write(self.manifest_path, self.manifest)
        return negotiated

    def close(self) -> None:
        try:
            if self._async is not None:
                self._async.close()
        finally:
            self.source.close()
            if self.manifest is not None and self._writer is not None:
                manifest_mod.finalize(
                    self.manifest, stats=self.source.stats(),
                    frames_written=self._writer.frames_written,
                    bytes_written=self._writer.bytes_written)
                self.manifest["segments"] = self._writer.segments
                self.manifest["writer"] = {
                    "queue_high_water": self._async.high_water if self._async else 0,
                    "queue_depth": self._queue_depth,
                    "frames_flagged": self._writer.frames_flagged,
                }
                manifest_mod.write(self.manifest_path, self.manifest)

    # ---------------------------------------------------------------------- capture

    def frames(self):
        """Capture and write frames, yielding each one after it is queued for writing.

        Yielding after queueing rather than before means a consumer of this generator
        cannot delay the write, only observe it. A consumer that wants to do real work
        per frame should take frames from a LatestFrame instead -- see ring.py -- because
        anything slow in this loop costs the recording, not the consumer.
        """
        for frame in self.source.frames(patience=self.patience):
            self._async.write(frame)
            yield frame

    def record(self, *, seconds: float | None = None,
               frames: int | None = None) -> dict:
        """Record for a duration or a frame count, whichever is given.

        Returns the stats dict. Raises NoSignal if nothing arrives for `patience`
        seconds, and StreamError if the device is unplugged mid-session -- in both cases
        after the manifest has been finalized by __exit__, so a partial recording is
        still readable.
        """
        deadline = time.monotonic() + seconds if seconds else None
        count = 0
        for _ in self.frames():
            count += 1
            if frames is not None and count >= frames:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
        return self.stats()

    def stats(self) -> dict:
        out = dict(self.source.stats())
        if self._writer is not None:
            out["written"] = self._writer.frames_written
            out["bytes"] = self._writer.bytes_written
            out["flagged"] = self._writer.frames_flagged
        if self._async is not None:
            out["queue_high_water"] = self._async.high_water
        return out


__all__ = ["Session", "NoSignal", "StreamError"]
