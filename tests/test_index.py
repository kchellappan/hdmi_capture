#!/usr/bin/env python3
"""The frame index: fixed-size records, random access, and lookup by timestamp.

The index is what makes a recording usable as a dataset rather than as a video, so its
invariants are worth stating explicitly: a record is exactly 32 bytes so that frame N is
at 32N, and a timestamp search returns the frame that was actually on screen -- which is
not the same as the nearest frame by interpolation once anything has been dropped.
"""
from __future__ import annotations

import os
import sys
import tempfile

import fake  # noqa: F401

from vcap.index import RECORD_SIZE, IndexReader, pack

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")


def main() -> int:
    check("a record is 32 bytes", RECORD_SIZE, 32)

    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "a.idx")
    period = 16_666_667
    with open(path, "wb") as fh:
        offset = 0
        for i in range(500):
            fh.write(pack(offset, 1000 + i, 1_000_000_000 + i * period, i, 0, 0))
            offset += 1000 + i

    with IndexReader(path) as ix:
        check("count", len(ix), 500)
        check("frame N is at byte 32N", os.path.getsize(path), 500 * 32)

        e = ix[250]
        check("entry index", e.index, 250)
        check("entry seq", e.seq, 250)
        check("entry length", e.length, 1250)
        check("entry timestamp", e.ts_mono_ns, 1_000_000_000 + 250 * period)
        check("negative indexing", ix[-1].index, 499)

        # An offset must be the sum of all preceding lengths; an off-by-one here reads
        # the tail of one frame and the head of the next, which decodes as a corrupt
        # image rather than as an error.
        expected_offset = sum(1000 + i for i in range(250))
        check("offset is cumulative", e.offset, expected_offset)

        t = ix[100].ts_mono_ns
        check("search finds an exact timestamp", ix.search(t), 100)
        check("search rounds down within a frame", ix.search(t + period - 1), 100)
        check("search finds the next frame", ix.search(t + period), 101)
        check("search before the recording gives -1",
              ix.search(999_999_999), -1)
        check("search after the recording gives the last frame",
              ix.search(9_999_999_999_999), 499)

        # nearest() rounds to whichever side is closer, which is what an aligner wants.
        check("nearest rounds down below the midpoint",
              ix.nearest(t + period // 2 - 1).index, 100)
        check("nearest rounds up above the midpoint",
              ix.nearest(t + period // 2 + 1).index, 101)

        check("no gaps in a uniform recording", ix.gaps(), [])

    # A gap: one interval of four frame periods where three were lost.
    path = os.path.join(tmp, "gap.idx")
    with open(path, "wb") as fh:
        ts = 1_000_000_000
        for i in range(50):
            fh.write(pack(i * 100, 100, ts, i, 0, 0))
            ts += period * (4 if i == 20 else 1)
    with IndexReader(path) as ix:
        gaps = ix.gaps()
        check("one gap found", len(gaps), 1)
        check("gap is at the right frame", gaps[0][0] if gaps else None, 20)
        # Interpolation would place this in the middle of the gap; the search must
        # return frame 20, which is the frame that was actually on screen.
        mid = ix[20].ts_mono_ns + period * 2
        check("a timestamp inside a gap resolves to the frame before it",
              ix.search(mid), 20)

    # A writer killed mid-record leaves a partial one. The whole records before it are
    # still good, and refusing to open the file would throw away a whole session.
    path = os.path.join(tmp, "torn.idx")
    with open(path, "wb") as fh:
        for i in range(10):
            fh.write(pack(i * 100, 100, 1_000_000_000 + i * period, i, 0, 0))
        fh.write(b"\x01\x02\x03\x04\x05")
    with IndexReader(path) as ix:
        check("a torn trailing record is ignored", len(ix), 10)
        check("the torn bytes are reported", ix.truncated_bytes, 5)
        check("the last whole record still reads", ix[9].seq, 9)

    # A session that saw no signal writes an empty index. mmap refuses a zero-length
    # file, so this path needs its own handling rather than an exception.
    path = os.path.join(tmp, "empty.idx")
    open(path, "wb").close()
    with IndexReader(path) as ix:
        check("an empty index reads as zero frames", len(ix), 0)
        check("nearest on an empty index is None", ix.nearest(0), None)

    import shutil
    shutil.rmtree(tmp)

    for line in FAILURES:
        print(f"  FAIL  {line}")
    if not FAILURES:
        print("  ok    index records, random access, timestamp search, torn tail")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
