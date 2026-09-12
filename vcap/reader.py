"""Reading a recording back.

The access patterns this is built for are a dataloader's: give me frame K of this
episode, and give me the frame that was on screen when this control sample was taken.
Both are O(log n) or better and neither decodes anything it was not asked for.

Frames come back encoded. Decoding is deliberately not done here -- see decode.py for
why that is the dependency boundary of this package, and docs/formats.md for what a
consumer is expected to do with a JPEG.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from . import manifest as manifest_mod
from .frame import Frame
from .index import Entry, IndexReader


@dataclass(frozen=True)
class Located:
    """A frame's index entry plus which segment it lives in."""
    entry: Entry
    segment: int
    global_index: int


class Recording:
    """A session on disk, opened for random access.

    Segments are presented as one continuous sequence: `recording[n]` is the nth frame of
    the session regardless of which file it landed in, because a segment boundary is an
    artefact of how the recording was written and has no meaning to a consumer.
    """

    def __init__(self, path: str):
        """`path` is either the session directory or its manifest.json."""
        if os.path.isdir(path):
            self.directory = path
            manifest_path = os.path.join(path, "manifest.json")
        else:
            self.directory = os.path.dirname(os.path.abspath(path))
            manifest_path = path
        self.manifest = manifest_mod.read(manifest_path)

        version = self.manifest.get("format_version")
        if version is not None and version > manifest_mod.FORMAT_VERSION:
            raise ValueError(
                f"recording is format version {version}; this build reads up to "
                f"{manifest_mod.FORMAT_VERSION}")

        self._indexes: list[IndexReader] = []
        self._streams: list = []
        # Cumulative frame count before each segment, for mapping a global index to a
        # (segment, local index) pair without scanning.
        self._starts: list[int] = []
        total = 0
        for seg in self.manifest.get("segments", []):
            index_path = os.path.join(self.directory, seg["index"])
            stream_path = os.path.join(self.directory, seg["stream"])
            if not os.path.exists(index_path):
                # A session killed before rollover completed can list a segment it never
                # wrote. Skipping it is right; refusing to open the recording is not.
                continue
            ix = IndexReader(index_path)
            self._indexes.append(ix)
            self._streams.append(open(stream_path, "rb"))
            self._starts.append(total)
            total += len(ix)
        self.count = total

    def __enter__(self) -> "Recording":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        for ix in self._indexes:
            ix.close()
        for fh in self._streams:
            fh.close()
        self._indexes.clear()
        self._streams.clear()

    def __len__(self) -> int:
        return self.count

    @property
    def timestamps_are_monotonic(self) -> bool:
        """Whether frame timestamps can be compared with time.monotonic_ns() here.

        False means the driver did not stamp buffers on CLOCK_MONOTONIC, which makes
        alignment against control samples invalid. Checked rather than assumed because
        the answer is a property of the driver, not of this format.
        """
        return bool(self.manifest.get("timebase", {})
                    .get("comparable_with_monotonic_ns"))

    # ------------------------------------------------------------------- locating frames

    def _locate(self, global_index: int) -> Located:
        if global_index < 0:
            global_index += self.count
        if not 0 <= global_index < self.count:
            raise IndexError(global_index)
        seg = 0
        for i, start in enumerate(self._starts):
            if global_index >= start:
                seg = i
            else:
                break
        local = global_index - self._starts[seg]
        return Located(self._indexes[seg][local], seg, global_index)

    def entry(self, global_index: int) -> Entry:
        return self._locate(global_index).entry

    def __getitem__(self, global_index: int) -> Frame:
        found = self._locate(global_index)
        return self._load(found)

    def _load(self, found: Located) -> Frame:
        fh = self._streams[found.segment]
        fh.seek(found.entry.offset)
        data = fh.read(found.entry.length)
        if len(data) != found.entry.length:
            raise IOError(
                f"frame {found.global_index}: index says {found.entry.length} bytes at "
                f"offset {found.entry.offset}, stream has {len(data)}. The recording is "
                f"truncated -- run tools/vcap-verify")
        return Frame(data=data, ts_mono_ns=found.entry.ts_mono_ns,
                     seq=found.entry.seq, flags=found.entry.flags,
                     dropped_before=found.entry.dropped_before)

    def __iter__(self):
        for i in range(self.count):
            yield self[i]

    def index_at(self, ts_mono_ns: int) -> int:
        """Global index of the last frame at or before `ts_mono_ns`; -1 if before all."""
        for seg in reversed(range(len(self._indexes))):
            ix = self._indexes[seg]
            if len(ix) and ix[0].ts_mono_ns <= ts_mono_ns:
                local = ix.search(ts_mono_ns)
                return self._starts[seg] + local if local >= 0 else -1
        return -1

    def at(self, ts_mono_ns: int, *, tolerance_ns: int | None = None) -> Frame | None:
        """The frame nearest `ts_mono_ns`, or None if nothing is within tolerance.

        `tolerance_ns` has no default on purpose. What counts as "close enough" depends
        on what the frame is for -- a policy trained at 10 Hz tolerates far more slack
        than one at 60 -- and a default here would silently become that decision for
        every caller. Passing None returns the nearest frame at any distance.
        """
        i = self.index_at(ts_mono_ns)
        candidates = [j for j in (i, i + 1) if 0 <= j < self.count]
        if not candidates:
            if self.count == 0:
                return None
            candidates = [0 if i < 0 else self.count - 1]
        best = min(candidates, key=lambda j: abs(self.entry(j).ts_mono_ns - ts_mono_ns))
        if tolerance_ns is not None:
            if abs(self.entry(best).ts_mono_ns - ts_mono_ns) > tolerance_ns:
                return None
        return self[best]

    def span_ns(self) -> tuple[int, int] | None:
        if self.count == 0:
            return None
        return self.entry(0).ts_mono_ns, self.entry(self.count - 1).ts_mono_ns

    def to_realtime_ns(self, ts_mono_ns: int) -> int | None:
        """Convert a frame timestamp to wall clock, using the pair taken at session start.

        Returns None when the manifest has no clock pair. The conversion is only as good
        as that single sample: it assumes the two clocks did not drift relative to one
        another during the session, which is true to well under a millisecond over the
        length of a demonstration and not worth correcting for.
        """
        started = self.manifest.get("started") or {}
        mono, real = started.get("monotonic_ns"), started.get("realtime_ns")
        if mono is None or real is None:
            return None
        return real + (ts_mono_ns - mono)

    def flagged(self) -> list[Entry]:
        """Every frame the capture path marked as errored, corrupt, or preceded by a gap.

        This is what a dataset builder should consult before using a recording. The
        frames are present in the stream regardless -- omitting them at capture time
        would have made the recording look complete when it was not.
        """
        out = []
        for i in range(self.count):
            e = self.entry(i)
            if e.flags:
                out.append(e)
        return out
