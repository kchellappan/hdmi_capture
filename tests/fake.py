"""Synthetic frames, so the storage path is testable without a capture card.

Everything from SegmentWriter onwards is hardware-independent: it takes Frame objects and
produces files. The only thing it needs from a real card is plausible JPEGs, and a
minimal but structurally valid JPEG is short enough to build by hand. That is what makes
the whole write-index-read-verify path exercisable on a CI runner with no USB.

What this cannot stand in for: format negotiation, buffer mmap, kernel timestamps, and
every hardware quirk in docs/hardware.md. Those need the card, and a test that claims to
cover them without it would be lying.
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "vcap_py"))

from vcap.frame import Frame  # noqa: E402


def make_jpeg(width: int = 64, height: int = 48, payload: int = 128,
              *, complete: bool = True, headless: bool = False) -> bytes:
    """A structurally valid JPEG: SOI, a baseline SOF0, filler, and optionally EOI.

    Not decodable -- there is no Huffman table or scan data -- but it has exactly the
    structure the marker walker in vcap.frame reads, which is what is under test.

    The two ways a real frame arrives broken are modelled separately, because they have
    different causes and only one of them is common:

    * headless=True drops everything before the scan data, leaving a valid EOI. This is
      the stream-start fragment: the card transmits continuously and STREAMON begins
      assembling mid-frame. Measured at 33 of 34 stream starts.
    * complete=False drops the trailing EOI. This is a source cutting out mid-frame.
    """
    out = bytearray(b"\xff\xd8")
    # SOF0: length 17, 8-bit precision, height, width, 3 components.
    sof = bytearray(b"\xff\xc0\x00\x11\x08")
    sof += height.to_bytes(2, "big")
    sof += width.to_bytes(2, "big")
    sof += b"\x03"
    sof += b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    out += sof
    # A comment segment as filler, so frames can be given a realistic size.
    if payload > 0:
        body = b"\x00" * payload
        out += b"\xff\xfe" + (len(body) + 2).to_bytes(2, "big") + body
    if complete:
        out += b"\xff\xd9"
    if headless:
        # Keep only the back portion, as the driver does when it starts mid-frame: no
        # SOI, no SOF, but the frame's own valid ending.
        return bytes(out[len(out) // 2:])
    return bytes(out)


def frames(count: int, *, start_ns: int = 1_000_000_000, period_ns: int = 16_666_667,
           width: int = 64, height: int = 48, flag_first: bool = False,
           gap_at: int | None = None, gap_frames: int = 3):
    """A sequence of Frames resembling what VideoSource yields.

    `flag_first` reproduces this card's behaviour at STREAMON: the first buffer holds a
    headless fragment of a frame already in flight. `gap_at` drops frames from the
    sequence counter to simulate the driver losing them, which is what FLAG_GAP_BEFORE
    records.
    """
    from vcap.frame import FLAG_CORRUPT, FLAG_GAP_BEFORE

    seq = 0
    ts = start_ns
    for i in range(count):
        flags = 0
        dropped = 0
        headless = False
        if i == 0 and flag_first:
            flags |= FLAG_CORRUPT
            headless = True
        if gap_at is not None and i == gap_at:
            seq += gap_frames
            ts += period_ns * gap_frames
            dropped = gap_frames
            flags |= FLAG_GAP_BEFORE
        data = make_jpeg(width, height, payload=100 + (i % 50), headless=headless)
        yield Frame(data=data, ts_mono_ns=ts, seq=seq, flags=flags,
                    dropped_before=dropped)
        seq += 1
        ts += period_ns


class FakeDevice:
    """Stands in for a Device where only its identity is needed.

    manifest.build() asks a device to describe itself and nothing more, so a recording
    can be constructed for tests without opening anything.
    """

    def __init__(self, path: str = "/dev/v4l/by-id/fake-video-index0"):
        self.path = path
        self.node = "/dev/video99"

    def identity(self) -> dict:
        return {"path": self.path, "node_at_capture": self.node,
                "driver": "fake", "card": "fake capture", "bus_info": "fake",
                "usb": None}


class FakeNegotiated:
    """The subset of source.Negotiated that a manifest records."""

    def __init__(self, *, width: int = 64, height: int = 48, fps: float = 60.0,
                 pixelformat: str = "MJPG", monotonic: bool = True):
        self.pixelformat = pixelformat
        self.width = width
        self.height = height
        self.fps_requested = fps
        self.fps_granted = fps
        self.timestamp_clock = "monotonic" if monotonic else "unknown"
        self.timestamp_source = "start-of-frame"

    @property
    def timestamps_are_monotonic(self) -> bool:
        return self.timestamp_clock == "monotonic"


def write_recording(directory: str, count: int = 100, *, segment_bytes: int = 0,
                    flag_first: bool = False, gap_at: int | None = None,
                    period_ns: int = 16_666_667, monotonic: bool = True,
                    finalize: bool = True) -> dict:
    """Build a complete on-disk recording from synthetic frames.

    Returns the manifest. With finalize=False the end-of-session fields are left out,
    which is what a recording interrupted by a crash looks like on disk.
    """
    from vcap import manifest as manifest_mod
    from vcap.writer import SegmentWriter

    device = FakeDevice()
    negotiated = FakeNegotiated(monotonic=monotonic)
    writer = SegmentWriter(directory, "session", segment_bytes=segment_bytes)
    written = list(frames(count, period_ns=period_ns, flag_first=flag_first,
                          gap_at=gap_at))
    manifest = manifest_mod.build(
        session="session", device=device, negotiated=negotiated,
        requested={"pixelformat": "MJPG", "width": 64, "height": 48, "fps": 60.0},
        stream_file="session.mjpg", index_file="session.idx")
    for frame in written:
        writer.write(frame)
    writer.close()
    manifest["segments"] = writer.segments
    if finalize:
        manifest_mod.finalize(manifest, stats={"frames": len(written),
                                               "driver_dropped": 0,
                                               "driver_errors": 0, "corrupt": 0},
                              frames_written=writer.frames_written,
                              bytes_written=writer.bytes_written)
    import os
    manifest_mod.write(os.path.join(directory, "manifest.json"), manifest)
    return manifest
