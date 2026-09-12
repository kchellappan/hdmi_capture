#!/usr/bin/env python3
"""The JPEG marker walker: geometry and completeness without a decoder.

These matter because both are load-bearing. jpeg_dimensions is how vcap-verify notices
that an HDMI source changed resolution mid-session, and looks_like_jpeg is how a partial
frame gets flagged instead of silently entering a dataset.
"""
from __future__ import annotations

import sys

from fake import make_jpeg

from vcap.frame import jpeg_dimensions, looks_like_jpeg

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")


def main() -> int:
    check("dimensions of a 64x48 frame", jpeg_dimensions(make_jpeg(64, 48)), (64, 48))
    check("dimensions of 1920x1080", jpeg_dimensions(make_jpeg(1920, 1080)),
          (1920, 1080))
    check("dimensions of 2560x1600", jpeg_dimensions(make_jpeg(2560, 1600)),
          (2560, 1600))

    # A frame truncated before the header has no geometry to report, and saying so is the
    # difference between flagging it and recording a wrong size.
    truncated = make_jpeg(1920, 1080)[:4]
    check("truncated before SOF yields None", jpeg_dimensions(truncated), None)
    check("empty input yields None", jpeg_dimensions(b""), None)
    check("not a JPEG yields None", jpeg_dimensions(b"\x89PNG\r\n\x1a\n"), None)

    check("complete JPEG is complete", looks_like_jpeg(make_jpeg(64, 48)), True)
    check("frame without EOI is incomplete",
          looks_like_jpeg(make_jpeg(64, 48, complete=False)), False)
    # The common case by far: the stream-start fragment, which has a perfectly valid EOI
    # and is missing its head. A check that tested only the tail -- the intuitive choice,
    # since signal loss costs the tail -- would pass this and let an undecodable frame
    # into a dataset unflagged.
    headless = make_jpeg(64, 48, headless=True)
    check("a headless fragment is not a whole JPEG", looks_like_jpeg(headless), False)
    check("and it does end with a valid EOI",
          headless[-2:], b"\xff\xd9")
    check("a headless fragment has no readable geometry",
          jpeg_dimensions(headless), None)
    check("empty is not a JPEG", looks_like_jpeg(b""), False)
    check("SOI alone is not a JPEG", looks_like_jpeg(b"\xff\xd8"), False)

    # Padding between segments is legal and encoded as repeated 0xFF. A walker that does
    # not skip it stops at the first pad and reports no geometry.
    padded = bytearray(make_jpeg(800, 600))
    padded[2:2] = b"\xff\xff\xff"
    check("padding bytes before SOF are skipped",
          jpeg_dimensions(bytes(padded)), (800, 600))

    # DHT shares the 0xC0-0xCF range with the start-of-frame markers but is not one. A
    # walker that treats 0xC4 as an SOF reads its table bytes as a resolution.
    with_dht = bytearray(b"\xff\xd8")
    with_dht += b"\xff\xc4\x00\x06\x00\x01\x02\x03"
    with_dht += make_jpeg(320, 240)[2:]
    check("DHT is not mistaken for a start-of-frame",
          jpeg_dimensions(bytes(with_dht)), (320, 240))

    for line in FAILURES:
        print(f"  FAIL  {line}")
    if not FAILURES:
        print("  ok    jpeg marker walking")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
