"""The frame index: what makes a recording usable as training data.

A recording is two files. `<name>.mjpg` holds the frames exactly as the card produced
them, concatenated, with nothing added. `<name>.idx` holds one fixed-size record per
frame saying where it is and when it happened.

Fixed-size records are the whole point. Frame N's record is at byte 32*N, so getting the
Kth frame of an episode is two seeks and no parsing, and finding the frame at a given
timestamp is a binary search over a memory-mapped file. That is the access pattern an ACT
or VLA dataloader has -- random access by index, and lookup by the timestamp of a control
sample -- and a container format does not give it for free: MP4 and MKV quantise
presentation timestamps to a fixed timebase, which loses the exact capture instant that
this repo exists to preserve.

The stream file stays a plain concatenation so that nothing about the recording depends
on this code being correct. If the index is lost, the frames can be recovered by scanning
for JPEG markers; if the format is ever outgrown, the frames are already in the most
portable form they could be in.
"""
from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass

# byte_offset, length, ts_mono_ns, seq, flags, dropped_before.
# Little-endian and explicitly unaligned, so the record is 32 bytes on every platform
# rather than whatever the local alignment rules produce.
RECORD = struct.Struct("<QIQIII")
RECORD_SIZE = RECORD.size
assert RECORD_SIZE == 32, RECORD_SIZE


@dataclass(frozen=True)
class Entry:
    index: int
    offset: int
    length: int
    ts_mono_ns: int
    seq: int
    flags: int
    dropped_before: int


def pack(offset: int, length: int, ts_mono_ns: int, seq: int, flags: int,
         dropped_before: int) -> bytes:
    return RECORD.pack(offset, length, ts_mono_ns, seq, flags, dropped_before)


class IndexReader:
    """Random access over a .idx file.

    Memory-mapped rather than read into a list: an hour of 60 Hz capture is 216000
    records, which is only 7 MB, but a dataloader opens many recordings at once and
    mapping them costs no resident memory per recording that is not actually touched.
    """

    def __init__(self, path: str):
        self.path = path
        self._file = open(path, "rb")
        size = os.fstat(self._file.fileno()).st_size
        if size == 0:
            # mmap of an empty file raises; an empty recording is a legitimate outcome
            # (a session that saw no signal) and should read back as zero frames.
            self._map = None
            self.count = 0
            return
        if size % RECORD_SIZE:
            # A truncated tail means the writer died mid-record. The whole records
            # before it are still valid, so report them and ignore the fragment rather
            # than refusing to open the recording.
            self.truncated_bytes = size % RECORD_SIZE
        else:
            self.truncated_bytes = 0
        self._map = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
        self.count = size // RECORD_SIZE

    def __enter__(self) -> "IndexReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._map is not None:
            self._map.close()
            self._map = None
        self._file.close()

    def __len__(self) -> int:
        return self.count

    def __getitem__(self, index: int) -> Entry:
        if index < 0:
            index += self.count
        if not 0 <= index < self.count:
            raise IndexError(index)
        at = index * RECORD_SIZE
        fields = RECORD.unpack_from(self._map, at)
        return Entry(index, *fields)

    def __iter__(self):
        for i in range(self.count):
            yield self[i]

    def timestamps(self) -> list[int]:
        return [self[i].ts_mono_ns for i in range(self.count)]

    def search(self, ts_mono_ns: int) -> int:
        """Index of the last frame captured at or before `ts_mono_ns`.

        Returns -1 when the timestamp precedes the recording. Binary search rather than
        interpolation: frame intervals are not uniform -- a dropped frame or a source
        renegotiating its mode leaves a real gap -- so treating the index as evenly
        spaced in time would silently return the wrong frame.
        """
        lo, hi = 0, self.count - 1
        best = -1
        while lo <= hi:
            mid = (lo + hi) // 2
            if self[mid].ts_mono_ns <= ts_mono_ns:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def nearest(self, ts_mono_ns: int) -> Entry | None:
        """The frame closest in time, before or after. None if the recording is empty.

        This is the primitive an aligner wants, but note that "closest" is not
        automatically "correct": a control sample 400 ms from any frame has a nearest
        frame too. Callers pairing controls with frames should check the resulting gap
        against a tolerance they chose deliberately.
        """
        if self.count == 0:
            return None
        i = self.search(ts_mono_ns)
        if i < 0:
            return self[0]
        if i >= self.count - 1:
            return self[self.count - 1]
        before, after = self[i], self[i + 1]
        if ts_mono_ns - before.ts_mono_ns <= after.ts_mono_ns - ts_mono_ns:
            return before
        return after

    def gaps(self, *, factor: float = 1.5) -> list[tuple[int, int, int]]:
        """Intervals longer than `factor` times the median, as (index, from_ns, to_ns).

        Reported from timestamps rather than from the dropped-frame counter because the
        two answer different questions: the counter says the driver lost frames, while a
        long interval also catches the card pausing, the source renegotiating, and the
        writer stalling.
        """
        if self.count < 3:
            return []
        ts = self.timestamps()
        deltas = sorted(b - a for a, b in zip(ts, ts[1:]))
        median = deltas[len(deltas) // 2]
        if median <= 0:
            return []
        threshold = median * factor
        return [(i, ts[i], ts[i + 1])
                for i in range(self.count - 1) if ts[i + 1] - ts[i] > threshold]
