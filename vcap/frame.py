"""What a captured frame is, and how to learn its geometry without decoding it."""
from __future__ import annotations

from dataclasses import dataclass

# Set when the driver flagged the buffer as containing an error, or when the JPEG it
# holds is not well formed. Such frames are still written to the recording -- a gap
# recorded as a gap is useful, a gap that silently shortens the file is not -- but a
# training pipeline needs to be able to exclude them.
FLAG_DRIVER_ERROR = 1 << 0
FLAG_CORRUPT = 1 << 1

# Set when the kernel's sequence counter jumped, meaning the driver dropped at least one
# frame before this one. The count is in Frame.dropped_before.
FLAG_GAP_BEFORE = 1 << 2


@dataclass(frozen=True)
class Frame:
    """One frame, exactly as the device produced it.

    `data` is the encoded payload -- for MJPG a complete JPEG, for YUYV raw pixels. It is
    a bytes object copied out of the mmap'd buffer, because the buffer is requeued to the
    driver immediately and its contents are then free to change underneath a reader. The
    copy is the reason a frame can outlive the loop that produced it.

    `ts_mono_ns` is the kernel's timestamp on CLOCK_MONOTONIC, not a userspace
    observation. It is directly comparable with time.monotonic_ns() on this machine, and
    therefore with the arrival times rpi_gamepad_bridge's CaptureReceiver records. See
    docs/timebase.md for what it does and does not mean.

    `seq` is the driver's frame counter. Consecutive frames differ by one; a larger step
    means frames were dropped before userspace saw them.
    """
    data: bytes
    ts_mono_ns: int
    seq: int
    flags: int = 0
    dropped_before: int = 0

    @property
    def ok(self) -> bool:
        return not self.flags & (FLAG_DRIVER_ERROR | FLAG_CORRUPT)

    def __len__(self) -> int:
        return len(self.data)


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width and height from a JPEG's frame header, without decoding it.

    Walks the marker segments looking for a start-of-frame. Returns None if the data is
    not a JPEG or is truncated before the header, which is how a corrupt frame from a
    capture card usually presents -- the card emits a valid prefix and then stops.

    This is here so that verifying a recording, or checking that the negotiated geometry
    is what the card actually sent, does not require a JPEG decoder.
    """
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return None
    i = 2
    n = len(data)
    while i + 3 < n:
        if data[i] != 0xFF:
            # Not at a marker: the stream is malformed or we walked into entropy-coded
            # data, which should not happen before a start-of-frame.
            return None
        marker = data[i + 1]
        # Padding bytes between segments are legal and encoded as repeated 0xFF.
        if marker == 0xFF:
            i += 1
            continue
        # Standalone markers carry no length field.
        if marker in (0x01, 0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 3 >= n:
            return None
        length = (data[i + 2] << 8) | data[i + 3]
        # SOF0..SOF15, excluding the define-huffman-table and arithmetic-coding markers
        # that share the range.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 >= n:
                return None
            height = (data[i + 5] << 8) | data[i + 6]
            width = (data[i + 7] << 8) | data[i + 8]
            return width, height
        if length < 2:
            return None
        i += 2 + length
    return None


def looks_like_jpeg(data: bytes) -> bool:
    """Whether the payload is a whole JPEG: a start-of-image *and* an end-of-image marker.

    Both ends are checked because fragments occur at both ends, for different reasons.

    A frame missing its head is what STREAMON produces on a capture card that transmits
    continuously: the driver begins assembling from the payload already in flight, so the
    first buffer holds the back portion of a frame -- no SOI, no SOF, but a perfectly
    valid EOI. Measured on this project's card, frame 0 was byte-for-byte the tail of
    frame 1. See docs/hardware.md.

    A frame missing its tail is what a source cutting out mid-frame produces.

    Checking only the tail would pass the first case, which is the common one.
    """
    return (len(data) >= 4 and data[0] == 0xFF and data[1] == 0xD8
            and data[-2] == 0xFF and data[-1] == 0xD9)
