"""Writing a recording to disk without dropping frames.

The recorder's contract is the opposite of the live viewer's: a missing frame corrupts an
episode, so this path never discards one on purpose. When the disk cannot keep up the
queue fills and the writer says so loudly rather than quietly skipping -- a recording with
an unannounced hole in it is worse than a failed recording, because it looks fine and
trains badly.

Frames are written exactly as the card produced them. No transcode, no re-encode, no
repacking. At 1080p60 MJPEG that is about 12 MB/s of sequential append, which any disk
handles; the same session re-encoded to H.264 in the capture path would cost real CPU and
would throw away the exact per-frame timestamps by quantising them to a container
timebase.
"""
from __future__ import annotations

import os
import queue
import threading

from . import index as index_mod
from .frame import Frame


class WriterOverrun(Exception):
    """The disk could not keep up and the queue filled.

    Raised rather than handled because there is no correct way to handle it here.
    Dropping frames silently is what this class exists to prevent, and choosing which
    frames to drop is not a decision a writer can make on a caller's behalf.
    """


class SegmentWriter:
    """Appends frames to a .mjpg/.idx pair, rolling over at a size limit.

    Rollover exists because a single file for a long session is awkward in ways that
    matter later: it cannot be moved off the machine until the session ends, a filesystem
    with a 4 GB limit refuses it outright, and one corrupt region costs the whole
    recording. Segments are numbered and the manifest lists them in order.

    Set `segment_bytes` to 0 to disable rollover.
    """

    DEFAULT_SEGMENT_BYTES = 2 * 1024 * 1024 * 1024

    def __init__(self, directory: str, name: str = "session", *,
                 segment_bytes: int | None = None, fsync_every: int = 0):
        self.directory = directory
        self.name = name
        self.segment_bytes = (self.DEFAULT_SEGMENT_BYTES if segment_bytes is None
                              else segment_bytes)
        # Two different durability questions, deliberately separated.
        #
        # Surviving *this process* dying is handled unconditionally, by flushing both
        # files to the OS after every frame -- see write(). That is cheap and there is no
        # reason to make it optional.
        #
        # Surviving the *machine* losing power is what fsync_every controls, and it is
        # not cheap: an fsync is a round trip to the device. Zero means "let the OS
        # decide", which risks only what is still in the page cache; the index is
        # append-only and whole records before the cut survive, so the loss is bounded
        # and the recording stays readable.
        self.fsync_every = fsync_every

        os.makedirs(directory, exist_ok=True)
        self.segments: list[dict] = []
        self._stream = None
        self._index = None
        self._segment_no = -1
        self._segment_offset = 0
        self._since_sync = 0

        self.frames_written = 0
        self.bytes_written = 0
        self.frames_flagged = 0
        self._roll()

    # ------------------------------------------------------------------------- segments

    def _paths(self, segment_no: int) -> tuple[str, str]:
        stem = self.name if segment_no == 0 else f"{self.name}.{segment_no:03d}"
        return (os.path.join(self.directory, f"{stem}.mjpg"),
                os.path.join(self.directory, f"{stem}.idx"))

    def _roll(self) -> None:
        self._close_segment()
        self._segment_no += 1
        stream_path, index_path = self._paths(self._segment_no)
        self._stream = open(stream_path, "wb", buffering=1024 * 1024)
        self._index = open(index_path, "wb", buffering=64 * 1024)
        self._segment_offset = 0
        self.segments.append({
            "number": self._segment_no,
            "stream": os.path.basename(stream_path),
            "index": os.path.basename(index_path),
            "frames": 0,
            "bytes": 0,
            "first_ts_mono_ns": None,
            "last_ts_mono_ns": None,
        })

    def _close_segment(self) -> None:
        for fh in (self._stream, self._index):
            if fh is not None:
                fh.flush()
                os.fsync(fh.fileno())
                fh.close()
        self._stream = self._index = None

    # ----------------------------------------------------------------------------- write

    def write(self, frame: Frame) -> None:
        if self.segment_bytes and self._segment_offset + len(frame) > self.segment_bytes:
            self._roll()

        self._stream.write(frame.data)
        self._index.write(index_mod.pack(
            self._segment_offset, len(frame), frame.ts_mono_ns, frame.seq,
            frame.flags, frame.dropped_before))

        seg = self.segments[-1]
        if seg["first_ts_mono_ns"] is None:
            seg["first_ts_mono_ns"] = frame.ts_mono_ns
        seg["last_ts_mono_ns"] = frame.ts_mono_ns
        seg["frames"] += 1
        seg["bytes"] += len(frame)

        self._segment_offset += len(frame)
        self.frames_written += 1
        self.bytes_written += len(frame)
        if frame.flags:
            self.frames_flagged += 1

        # Push both files to the OS after every frame. This is not fsync -- no platter
        # is touched and no round trip is paid -- it only moves bytes out of this
        # process's buffers into the page cache.
        #
        # It matters because the index is 32 bytes per frame against a 64 KB buffer,
        # which is 2048 frames: without this, a process killed with SIGKILL, or an OOM
        # kill, loses up to 34 seconds of index at 60 Hz while the frames it describes
        # are already on disk. The frames would survive and their timestamps would not,
        # and the timestamps are the reason this repo exists.
        #
        # Both are flushed, and the stream first, so the index never references bytes
        # the OS has not seen. Measured cost at 60 Hz: two syscalls per frame, no
        # change in effective rate or queue depth.
        self._stream.flush()
        self._index.flush()

        if self.fsync_every:
            self._since_sync += 1
            if self._since_sync >= self.fsync_every:
                self._stream.flush()
                os.fsync(self._stream.fileno())
                self._index.flush()
                os.fsync(self._index.fileno())
                self._since_sync = 0

    def close(self) -> None:
        self._close_segment()

    def __enter__(self) -> "SegmentWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class AsyncWriter:
    """A SegmentWriter on its own thread, so a disk hiccup does not stall capture.

    The capture loop must return to DQBUF promptly: there are only as many kernel buffers
    as were requested, and once they are all full the driver starts dropping frames. A
    synchronous write of 200 KB usually returns in microseconds, but "usually" is doing a
    lot of work there -- a filesystem flush, a full page cache, or a USB disk can block
    for tens of milliseconds, which at 60 Hz is enough to exhaust four buffers.

    The queue is bounded. An unbounded queue does not solve a slow disk, it converts it
    into unbounded memory growth and then an OOM kill partway through a session.
    """

    def __init__(self, writer: SegmentWriter, *, depth: int = 240):
        self.writer = writer
        self._queue: queue.Queue = queue.Queue(maxsize=depth)
        self._error: BaseException | None = None
        self._stop = threading.Event()
        self.high_water = 0
        self._thread = threading.Thread(target=self._run, name="vcap-writer",
                                        daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self.writer.write(item)
            except BaseException as exc:        # noqa: BLE001 - reported to the caller
                self._error = exc
                self._stop.set()
                return

    def write(self, frame: Frame) -> None:
        """Hand a frame to the writer thread. Raises if the thread has failed."""
        if self._error is not None:
            raise self._error
        depth = self._queue.qsize()
        if depth > self.high_water:
            self.high_water = depth
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            raise WriterOverrun(
                f"write queue full ({self._queue.maxsize} frames); the disk is not "
                f"keeping up with capture") from None

    def close(self) -> None:
        """Drain the queue, then close the underlying writer.

        Drains rather than discards: frames already accepted are already accounted for
        in nothing at all -- they exist only in this queue -- so dropping them at
        shutdown would lose the end of every session.
        """
        self._queue.put(None)
        self._thread.join(timeout=30.0)
        self.writer.close()
        if self._error is not None:
            raise self._error

    def __enter__(self) -> "AsyncWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
