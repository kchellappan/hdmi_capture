#!/usr/bin/env python3
"""Writing a recording and reading it back, including the ways it can go wrong.

Covers the whole storage path with synthetic frames: segment rollover, spanning segments
transparently on read, timestamp lookup across a session, an unfinalized manifest, and a
truncated stream. None of it needs a capture card, which is the point -- the storage
format is where a bug costs a dataset, and it must be testable in CI.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

import fake

from vcap.frame import FLAG_CORRUPT, FLAG_GAP_BEFORE
from vcap.reader import Recording
from vcap.writer import AsyncWriter, SegmentWriter, WriterOverrun

FAILURES: list[str] = []
PERIOD = 16_666_667


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")


def truthy(name: str, got) -> None:
    if not got:
        FAILURES.append(f"{name}: got {got!r}, want something truthy")


def main() -> int:
    tmp = tempfile.mkdtemp()

    # ---------------------------------------------------------- a plain round trip
    d = os.path.join(tmp, "plain")
    manifest = fake.write_recording(d, count=120)
    check("manifest counts the frames", manifest["frames_written"], 120)
    truthy("manifest has an effective rate", manifest["effective_fps"])

    with Recording(d) as rec:
        check("recording length", len(rec), 120)
        truthy("timestamps are declared monotonic", rec.timestamps_are_monotonic)
        first, last = rec[0], rec[-1]
        check("first frame is complete", first.ok, True)
        check("frames survive the round trip byte for byte",
              rec[60].data, list(fake.frames(120))[60].data)
        check("sequence numbers survive", rec[60].seq, 60)
        check("timestamps survive", rec[60].ts_mono_ns,
              1_000_000_000 + 60 * PERIOD)

        # The lookup a dataloader actually performs.
        target = rec.entry(75).ts_mono_ns
        check("lookup by exact timestamp", rec.index_at(target), 75)
        found = rec.at(target + 1_000_000, tolerance_ns=8_000_000)
        truthy("a nearby timestamp finds a frame", found is not None)
        check("and it is the right one", found.seq if found else None, 75)
        # A tolerance that cannot be met must return nothing rather than the nearest
        # frame; silently pairing a control with a frame 100 ms away is how a dataset
        # ends up teaching the wrong thing.
        #
        # The probe timestamp is past the end of the recording on purpose. An earlier
        # version of this test used "half a second after frame 75", which at 60 Hz lands
        # within 10 ns of frame 105 -- so the tolerance was legitimately met and the test
        # failed while the code was right.
        beyond = rec.entry(-1).ts_mono_ns + 1_000_000_000
        check("a timestamp outside tolerance returns None",
              rec.at(beyond, tolerance_ns=8_000_000), None)
        nearest = rec.at(beyond)
        truthy("with no tolerance the nearest frame is returned", nearest is not None)
        check("and the nearest is the last frame", nearest.seq if nearest else None, 119)

        real = rec.to_realtime_ns(target)
        truthy("monotonic converts to wall clock", real and real > 1_700_000_000_000_000)

    # ------------------------------------------------------------ flags are preserved
    d = os.path.join(tmp, "flagged")
    fake.write_recording(d, count=60, flag_first=True, gap_at=30)
    with Recording(d) as rec:
        flagged = rec.flagged()
        check("two frames are flagged", len(flagged), 2)
        check("the first frame is marked incomplete",
              bool(flagged[0].flags & FLAG_CORRUPT), True)
        check("the frame after the gap is marked",
              bool(flagged[1].flags & FLAG_GAP_BEFORE), True)
        check("the gap records how many were lost", flagged[1].dropped_before, 3)
        # The bad frames are still in the recording. Dropping them at capture time would
        # have made a recording with a hole look complete.
        check("flagged frames are still present", len(rec), 60)
        check("and still readable", len(rec[0].data) > 0, True)

    # ----------------------------------------------------------- segment rollover
    d = os.path.join(tmp, "segments")
    manifest = fake.write_recording(d, count=200, segment_bytes=2000)
    truthy("rollover produced several segments", len(manifest["segments"]) > 3)
    check("every frame is accounted for across segments",
          sum(s["frames"] for s in manifest["segments"]), 200)

    with Recording(d) as rec:
        check("segments read as one sequence", len(rec), 200)
        # A segment boundary is an artefact of writing and must not be visible here.
        originals = list(fake.frames(200))
        mismatched = [i for i in range(200) if rec[i].data != originals[i].data]
        check("no frame is corrupted by a segment boundary", mismatched, [])
        check("sequence numbers stay continuous across segments",
              [rec[i].seq for i in (0, 99, 100, 199)], [0, 99, 100, 199])
        target = rec.entry(150).ts_mono_ns
        check("timestamp lookup crosses segments", rec.index_at(target), 150)
        span = rec.span_ns()
        check("span covers the whole session",
              span, (rec.entry(0).ts_mono_ns, rec.entry(199).ts_mono_ns))

    # ------------------------------------------------- an unclean shutdown is readable
    d = os.path.join(tmp, "unclean")
    fake.write_recording(d, count=40, finalize=False)
    with Recording(d) as rec:
        check("an unfinalized recording still opens", len(rec), 40)
        check("and has no end record", "ended" in rec.manifest, False)

    # ------------------------------------------------------- a truncated stream errors
    d = os.path.join(tmp, "truncated")
    fake.write_recording(d, count=40)
    stream = os.path.join(d, "session.mjpg")
    with open(stream, "r+b") as fh:
        fh.truncate(os.path.getsize(stream) // 2)
    with Recording(d) as rec:
        # Reading a frame whose bytes are gone must fail loudly. Returning a short
        # buffer would hand a dataset a silently corrupt image.
        failed = False
        try:
            rec[39]
        except IOError:
            failed = True
        check("reading past a truncated stream raises", failed, True)
        check("frames before the cut still read", rec[0].ok, True)

    # ----------------------------------------------------- a full queue is not silent
    d = os.path.join(tmp, "overrun")
    writer = SegmentWriter(d, "session", segment_bytes=0)
    async_writer = AsyncWriter(writer, depth=1)
    overran = False
    try:
        # Nothing drains this faster than it fills; the point is that it raises rather
        # than dropping frames or growing without bound.
        for frame in fake.frames(5000):
            async_writer.write(frame)
    except WriterOverrun:
        overran = True
    try:
        async_writer.close()
    except Exception:
        pass
    check("a full write queue raises rather than dropping frames", overran, True)

    shutil.rmtree(tmp)

    for line in FAILURES:
        print(f"  FAIL  {line}")
    if not FAILURES:
        print("  ok    write and read back: round trip, flags, segments, truncation")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
