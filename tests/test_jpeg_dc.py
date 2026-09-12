#!/usr/bin/env python3
"""The DC-only JPEG decoder.

Tested against synthetic JPEGs whose DC coefficients are known exactly, rather than
against a captured frame. A real frame can only be checked for plausibility; a synthetic
one has a right answer for every block, which is what catches a decoder that desyncs
halfway through a scan or gets JPEG's sign convention backwards.

The first attempt at a fixture here was a frame captured from the card with nothing on its
HDMI input. It decodes to a uniform image, so it would have passed against a decoder that
returned a constant.
"""
from __future__ import annotations

import sys

import fake_jpeg

from vcap.jpeg_dc import JpegError, decode_dc

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")


def main() -> int:
    # Exact recovery, including both extremes and a pattern that changes every block --
    # a desync shows up as a shifted or garbage row rather than a subtle error.
    want = [[(x * 17 + y * 29) % 256 for x in range(10)] for y in range(6)]
    want[0][0], want[0][1] = 0, 255
    img = decode_dc(fake_jpeg.encode_dc_only(want))
    check("geometry in pixels", (img.full_width, img.full_height), (80, 48))
    check("geometry in blocks", (img.width, img.height), (10, 6))
    check("every block decodes to its exact value",
          [img.row(y) for y in range(img.height)], want)

    # The DC term is differential across the whole scan, so an error in one block shifts
    # every block after it. A long run of large alternating values is the case most
    # likely to expose a sign or extend() mistake.
    swing = [[0 if (x + y) % 2 else 255 for x in range(16)] for y in range(16)]
    img = decode_dc(fake_jpeg.encode_dc_only(swing))
    check("alternating extremes survive differential coding",
          [img.row(y) for y in range(img.height)], swing)

    # A value needing the full 11-bit DC size, to exercise the widest Huffman symbol.
    wide = [[128, 0, 255, 128]]
    img = decode_dc(fake_jpeg.encode_dc_only(wide))
    check("full-range swings", img.row(0), wide[0])

    # column_means is what the barcode reader is built on.
    grid = [[10] * 4 + [200] * 4 for _ in range(8)]
    img = decode_dc(fake_jpeg.encode_dc_only(grid))
    check("column means over a row range", img.column_means(2, 6), [10] * 4 + [200] * 4)
    check("column means clamp an out-of-range request",
          img.column_means(-5, 999), [10] * 4 + [200] * 4)

    # Refusals. Each of these would otherwise produce a plausible wrong image, which is
    # worse than an error for a measurement tool.
    for name, data in (
        ("not a JPEG", b"\x89PNG\r\n\x1a\n"),
        ("empty", b""),
        ("SOI only", b"\xff\xd8"),
    ):
        try:
            decode_dc(data)
            FAILURES.append(f"{name} was accepted")
        except JpegError:
            pass

    # A progressive JPEG must be named as such, not decoded into nonsense. Built by
    # swapping the SOF0 marker for SOF2 in a valid file.
    baseline = bytearray(fake_jpeg.encode_dc_only([[128, 128]]))
    at = baseline.find(b"\xff\xc0")
    baseline[at + 1] = 0xC2
    try:
        decode_dc(bytes(baseline))
        FAILURES.append("a progressive JPEG was accepted")
    except JpegError as exc:
        if "progressive" not in str(exc):
            FAILURES.append(f"progressive rejected with an unhelpful message: {exc}")

    # A frame truncated inside its scan is the common real case -- the card's stream-start
    # fragment. It must not raise: the reader feeds zeros and lets the caller decide,
    # because raising per frame would make the calibration loop unusable.
    whole = fake_jpeg.encode_dc_only([[100] * 8 for _ in range(8)])
    # Cut partway through the entropy-coded data, not through the header. Truncating by a
    # fraction of the file cuts before the scan on a small image, where the header is most
    # of the bytes -- which tests the wrong thing and was how this test first failed.
    scan_start = whole.find(b"\xff\xda")
    scan_start = whole.find(b"\x00", scan_start + 12) + 1
    try:
        truncated = decode_dc(whole[:scan_start + (len(whole) - scan_start) // 2])
        check("a truncated scan still yields an image of the right shape",
              (truncated.width, truncated.height), (8, 8))
    except JpegError as exc:
        FAILURES.append(f"a truncated scan raised instead of degrading: {exc}")

    for line in FAILURES:
        print(f"  FAIL  {line}")
    if not FAILURES:
        print("  ok    DC decoding: exact values, differential coding, refusals")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
