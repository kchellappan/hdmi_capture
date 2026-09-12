"""The capture loop: mmap'd buffers in, timestamped frames out.

This is the only place in the package that touches a streaming device, and it does one
job -- hand over each frame with the kernel's timestamp attached and an honest account of
what was lost. It does not decode, encode, convert, resample or drop. Those are policy,
and policy belongs to whoever is consuming frames, because the two consumers this repo
exists for want opposite things: a recorder must never drop a frame, and a live inference
loop must always prefer the newest one. See ring.py and writer.py.
"""
from __future__ import annotations

import ctypes
import errno
import mmap
import os
import select
import time
from dataclasses import dataclass

from . import v4l2
from .frame import FLAG_CORRUPT, FLAG_DRIVER_ERROR, FLAG_GAP_BEFORE, Frame, looks_like_jpeg


class NoSignal(Exception):
    """The device is streaming but produced no frame within the timeout.

    For an HDMI capture card this is the normal state when nothing is connected to the
    input, or when the source is powered off or renegotiating a mode. It is not a
    hardware fault and callers usually want to wait and retry rather than exit.
    """


class StreamError(Exception):
    """The device stopped being usable -- typically unplugged mid-stream (ENODEV)."""


@dataclass(frozen=True)
class Negotiated:
    """What the driver actually granted, which is not always what was asked for.

    S_FMT is a negotiation: the driver is entitled to return something other than the
    request and does so silently. Reading back what was granted is the only way to know
    what is being recorded, so this is what goes in the manifest -- never the request.
    """
    pixelformat: str
    width: int
    height: int
    sizeimage: int
    fps_requested: float | None
    fps_granted: float | None
    timestamp_clock: str
    timestamp_source: str

    @property
    def timestamps_are_monotonic(self) -> bool:
        return self.timestamp_clock == "monotonic"


def _describe_timestamp(flags: int) -> tuple[str, str]:
    clock = {
        v4l2.BUF_FLAG_TIMESTAMP_UNKNOWN: "unknown",
        v4l2.BUF_FLAG_TIMESTAMP_MONOTONIC: "monotonic",
        v4l2.BUF_FLAG_TIMESTAMP_COPY: "copy",
    }.get(flags & v4l2.BUF_FLAG_TIMESTAMP_MASK, "unrecognised")
    source = {
        v4l2.BUF_FLAG_TSTAMP_SRC_EOF: "end-of-frame",
        v4l2.BUF_FLAG_TSTAMP_SRC_SOF: "start-of-frame",
    }.get(flags & v4l2.BUF_FLAG_TSTAMP_SRC_MASK, "unrecognised")
    return clock, source


class VideoSource:
    """A streaming V4L2 capture device.

    Use it as a context manager; the buffers are mmap'd and the stream is started on
    entry and torn down on exit, including when the loop raises.

        with VideoSource(dev, "MJPG", 1920, 1080, fps=60) as src:
            for frame in src.frames():
                ...

    `buffers` is the number of kernel buffers queued. Four is the default because it is
    enough to cover a scheduling hiccup at 60 Hz without adding latency that a live
    consumer would feel: each buffer is one frame period of slack, so four is ~66 ms.
    Raising it makes the recorder more tolerant of a slow disk and makes a live viewer
    lag further behind.
    """

    def __init__(self, device, pixelformat: str = "MJPG", width: int = 1920,
                 height: int = 1080, *, fps: float | None = None, buffers: int = 4):
        self.device = device
        self.requested = (pixelformat, width, height, fps)
        self.n_buffers = buffers
        self.negotiated: Negotiated | None = None
        self._fd = -1
        self._maps: list[mmap.mmap] = []
        self._streaming = False
        self._last_seq: int | None = None
        # Counters. Every one of these is something observed, not inferred: see
        # docs/timebase.md on why "frames the driver dropped" is knowable and "frames the
        # card dropped" is not.
        self.frames_yielded = 0
        self.driver_dropped = 0
        self.driver_errors = 0
        self.corrupt = 0

    # ------------------------------------------------------------------ setup / teardown

    def __enter__(self) -> "VideoSource":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def open(self) -> Negotiated:
        pixelformat, width, height, fps = self.requested
        self._fd = os.open(self.device.node, os.O_RDWR | os.O_NONBLOCK)
        try:
            granted = self._set_format(pixelformat, width, height)
            granted_fps = self._set_rate(fps) if fps else None
            self._request_buffers()
            self._map_buffers()
            self._queue_all()
            self._stream_on()
            clock, source = self._timestamp_flags()
            self.negotiated = Negotiated(
                pixelformat=granted[0], width=granted[1], height=granted[2],
                sizeimage=granted[3], fps_requested=fps, fps_granted=granted_fps,
                timestamp_clock=clock, timestamp_source=source)
            return self.negotiated
        except Exception:
            self.close()
            raise

    def _set_format(self, pixelformat: str, width: int, height: int):
        fmt = v4l2.Format(type=v4l2.BUF_TYPE_VIDEO_CAPTURE)
        fmt.pix.width = width
        fmt.pix.height = height
        fmt.pix.pixelformat = v4l2.fourcc_to_int(pixelformat)
        fmt.pix.field = 1                          # V4L2_FIELD_NONE: progressive
        v4l2.ioctl(self._fd, v4l2.VIDIOC_S_FMT, fmt)
        return (v4l2.fourcc(fmt.pix.pixelformat), fmt.pix.width, fmt.pix.height,
                fmt.pix.sizeimage)

    def _set_rate(self, fps: float) -> float | None:
        """Ask for a frame rate. Advisory: a UVC device may ignore it entirely."""
        parm = v4l2.StreamParm(type=v4l2.BUF_TYPE_VIDEO_CAPTURE)
        try:
            v4l2.ioctl(self._fd, v4l2.VIDIOC_G_PARM, parm)
        except OSError:
            return None
        parm.capture.timeperframe.numerator = 1
        parm.capture.timeperframe.denominator = int(round(fps))
        try:
            v4l2.ioctl(self._fd, v4l2.VIDIOC_S_PARM, parm)
        except OSError:
            return None
        return parm.capture.timeperframe.as_hz()

    def _request_buffers(self) -> None:
        req = v4l2.RequestBuffers(count=self.n_buffers,
                                  type=v4l2.BUF_TYPE_VIDEO_CAPTURE,
                                  memory=v4l2.MEMORY_MMAP)
        v4l2.ioctl(self._fd, v4l2.VIDIOC_REQBUFS, req)
        if req.count < 1:
            raise StreamError("driver granted no buffers")
        # The driver may grant fewer than requested; the surplus indices are not valid.
        self.n_buffers = req.count

    def _map_buffers(self) -> None:
        for index in range(self.n_buffers):
            buf = v4l2.Buffer(index=index, type=v4l2.BUF_TYPE_VIDEO_CAPTURE,
                              memory=v4l2.MEMORY_MMAP)
            v4l2.ioctl(self._fd, v4l2.VIDIOC_QUERYBUF, buf)
            self._maps.append(mmap.mmap(self._fd, buf.length,
                                        mmap.MAP_SHARED, mmap.PROT_READ,
                                        offset=buf.m.offset))

    def _queue_all(self) -> None:
        for index in range(self.n_buffers):
            self._queue(index)

    def _queue(self, index: int) -> None:
        buf = v4l2.Buffer(index=index, type=v4l2.BUF_TYPE_VIDEO_CAPTURE,
                          memory=v4l2.MEMORY_MMAP)
        v4l2.ioctl(self._fd, v4l2.VIDIOC_QBUF, buf)

    def _stream_on(self) -> None:
        arg = ctypes.c_int(v4l2.BUF_TYPE_VIDEO_CAPTURE)
        v4l2.ioctl(self._fd, v4l2.VIDIOC_STREAMON, arg)
        self._streaming = True

    def _timestamp_flags(self) -> tuple[str, str]:
        """Which clock the driver will stamp buffers with, read before the first frame.

        QUERYBUF reports the flags a buffer carries, and the timestamp-source bits are
        set by the driver at setup rather than per frame. Reading them here means a
        recording can record its own time semantics even if it captures zero frames.
        """
        buf = v4l2.Buffer(index=0, type=v4l2.BUF_TYPE_VIDEO_CAPTURE,
                          memory=v4l2.MEMORY_MMAP)
        try:
            v4l2.ioctl(self._fd, v4l2.VIDIOC_QUERYBUF, buf)
        except OSError:
            return "unknown", "unknown"
        return _describe_timestamp(buf.flags)

    def close(self) -> None:
        if self._streaming:
            try:
                arg = ctypes.c_int(v4l2.BUF_TYPE_VIDEO_CAPTURE)
                v4l2.ioctl(self._fd, v4l2.VIDIOC_STREAMOFF, arg)
            except OSError:
                pass                   # already gone; nothing to salvage
            self._streaming = False
        for m in self._maps:
            try:
                m.close()
            except (OSError, BufferError):
                pass
        self._maps.clear()
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    # ---------------------------------------------------------------------- the hot path

    def read(self, timeout: float = 2.0) -> Frame:
        """One frame. Raises NoSignal on timeout, StreamError if the device went away.

        The buffer is copied and requeued before returning, so the frame stays valid
        after the driver reuses the buffer. At 1080p MJPEG that copy is on the order of
        200 KB -- cheap enough that the alternative, lending out a view into a live
        buffer, is not worth the class of bug it invites.
        """
        if not self._streaming:
            raise StreamError("not streaming")
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if not ready:
            raise NoSignal(
                f"no frame within {timeout:g}s -- nothing connected to the capture "
                f"card's input, or the source is off")

        buf = v4l2.Buffer(index=0, type=v4l2.BUF_TYPE_VIDEO_CAPTURE,
                          memory=v4l2.MEMORY_MMAP)
        try:
            v4l2.ioctl(self._fd, v4l2.VIDIOC_DQBUF, buf)
        except OSError as exc:
            if exc.errno == errno.EAGAIN:
                # select() said readable but the buffer was not ready. Treat as a
                # timeout rather than an error: it is a race, not a fault.
                raise NoSignal("device reported ready then had nothing") from exc
            if exc.errno in (errno.ENODEV, errno.EIO):
                raise StreamError(f"device went away mid-stream: {exc}") from exc
            raise

        try:
            flags = 0
            if buf.flags & v4l2.BUF_FLAG_ERROR:
                flags |= FLAG_DRIVER_ERROR
                self.driver_errors += 1

            data = self._maps[buf.index][:buf.bytesused]

            dropped = 0
            if self._last_seq is not None:
                dropped = max(0, buf.sequence - self._last_seq - 1)
                if dropped:
                    flags |= FLAG_GAP_BEFORE
                    self.driver_dropped += dropped
            self._last_seq = buf.sequence

            # Only meaningful for MJPEG; for raw formats the payload has no structure
            # to check, and the driver pads a short frame to sizeimage rather than
            # reporting it, so there is no signal to test.
            #
            # Expect this to fire on the first frame of nearly every stream: the card
            # transmits continuously and STREAMON lands mid-frame. See docs/hardware.md.
            if self.negotiated and self.negotiated.pixelformat == "MJPG":
                if not looks_like_jpeg(data):
                    flags |= FLAG_CORRUPT
                    self.corrupt += 1

            self.frames_yielded += 1
            return Frame(data=data, ts_mono_ns=buf.timestamp_ns(), seq=buf.sequence,
                         flags=flags, dropped_before=dropped)
        finally:
            self._queue(buf.index)

    def frames(self, timeout: float = 2.0, *, patience: float | None = None):
        """Frames, indefinitely.

        With `patience` set, a NoSignal is swallowed and retried until that many seconds
        have passed with nothing arriving -- which is what a recorder wants when an HDMI
        source may briefly renegotiate a mode mid-session. Without it, the first timeout
        propagates.
        """
        waiting_since: float | None = None
        while True:
            try:
                frame = self.read(timeout)
            except NoSignal:
                if patience is None:
                    raise
                now = time.monotonic()
                waiting_since = waiting_since or now
                if now - waiting_since >= patience:
                    raise
                continue
            waiting_since = None
            yield frame

    def stats(self) -> dict:
        """Counters, restricted to things actually observed.

        Deliberately absent: any figure for frames the capture card itself dropped
        before the kernel saw them. The USB payload carries no sequence number this code
        can check, so such a number would be a guess -- and a guess in a health readout
        is worse than a blank.
        """
        return {
            "frames": self.frames_yielded,
            "driver_dropped": self.driver_dropped,
            "driver_errors": self.driver_errors,
            "corrupt": self.corrupt,
        }
