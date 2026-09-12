#!/usr/bin/env python3
"""The bar pattern that carries a timestamp through an HDMI round trip.

Two layers are tested separately: the geometry, against synthetic column means at several
widths, and the whole path, by encoding a pattern into a real JPEG and decoding it back
through the DC decoder. The second is what proves the tool would work if a display were
attached -- everything except the display, the cable and the card.
"""
from __future__ import annotations

import sys

import fake_jpeg

from vcap.barcode import (BAR_WIDTH, BAND_HEIGHT, BAND_TOP, DATA_BITS, TOTAL_BARS,
                          WRAP_MS, DecodeError, checksum, decode_columns,
                          decode_dc_image, encode, reconstruct_ms)
from vcap.jpeg_dc import decode_dc

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")


def render_bars(bars: list[bool], width: int = 240, white: int = 235, black: int = 18,
                noise: int = 0) -> list[int]:
    out = []
    for x in range(width):
        index = int(((x + 0.5) / width) / BAR_WIDTH)
        level = (white if bars[index] else black) if index < TOTAL_BARS else 128
        out.append(max(0, min(255, level + (noise if x % 3 == 0 else -noise))))
    return out


def render_columns(value: int, **kwargs) -> list[int]:
    return render_bars(encode(value), **kwargs)


def main() -> int:
    check("a pattern is two reference bars plus data and checksum",
          len(encode(0)), TOTAL_BARS)
    check("the first reference bar is white", encode(0)[0], True)
    check("the second is black", encode(0)[1], False)

    for value in (0, 1, 0xFFFFF, 123456, 999999, 524287):
        check(f"round trip {value}", decode_columns(render_columns(value)), value)

    # Resolution independence. The pattern is defined in fractions of screen width, so it
    # has to decode at whatever the card delivers -- including a width that is not a
    # tidy multiple of the bar size.
    for width in (160, 240, 203, 320):
        check(f"decodes at DC width {width}",
              decode_columns(render_columns(777777, width=width)), 777777)

    # Levels a capture card actually returns: compressed range, not 0 and 255.
    check("survives a washed-out capture",
          decode_columns(render_columns(424242, white=190, black=70)), 424242)
    check("survives per-column noise",
          decode_columns(render_columns(123456, noise=30)), 123456)

    # Refusals. A latency measurement that silently invents a number is worse than one
    # that reports nothing.
    for name, columns in (
        ("a blank frame", [128] * 240),
        ("an all-white frame", [255] * 240),
        ("too few columns to resolve bars", [0, 255] * 10),
    ):
        try:
            decode_columns(columns)
            FAILURES.append(f"{name} was decoded instead of rejected")
        except DecodeError:
            pass

    # A single misread bar must be caught rather than yielding a wrong timestamp. That is
    # the failure that corrupts a measurement instead of failing it: one flipped bit moves
    # the answer by a power of two milliseconds, which looks entirely plausible.
    #
    # Every data and checksum bar is flipped in turn, one at a time. All of them must be
    # rejected -- a flip in the data breaks the checksum, and a flip in the checksum
    # breaks it just as surely.
    missed = []
    for index in range(2, TOTAL_BARS):
        bars = encode(0b10101010101010101010)
        bars[index] = not bars[index]
        try:
            decode_columns(render_bars(bars))
            missed.append(index)
        except DecodeError:
            pass
    check("every single-bar flip is caught by the checksum", missed, [])

    check("checksum is four bits", checksum(0xFFFFF) <= 0xF, True)

    # Reconstructing the full timestamp from the low bits, including across the wrap.
    for reference, latency in ((1_000_000.0, 45.0), (float(WRAP_MS) + 3.0, 40.0),
                               (float(WRAP_MS * 7) - 2.0, 55.0)):
        painted = reference - latency
        got = reconstruct_ms(int(painted) % WRAP_MS, reference)
        if abs((reference - got) - latency) > 1.0:
            FAILURES.append(f"reconstruct at {reference}: off by "
                            f"{abs((reference - got) - latency):.1f} ms")

    # The whole path, through a real JPEG: encode a pattern, compress it, decode the DC
    # coefficients, read the value back. Everything the tool does except the display,
    # the cable and the capture card.
    for value in (0, 314159, 0xFFFFF):
        data = fake_jpeg.encode_bars(encode(value), 240, 135, BAR_WIDTH,
                                     BAND_TOP, BAND_HEIGHT)
        image = decode_dc(data)
        try:
            check(f"end to end through a JPEG, value {value}",
                  decode_dc_image(image), value)
        except DecodeError as exc:
            FAILURES.append(f"end to end {value} failed: {exc}")

    # And at a different capture geometry, since the card scales.
    data = fake_jpeg.encode_bars(encode(271828), 160, 90, BAR_WIDTH, BAND_TOP, BAND_HEIGHT)
    try:
        check("end to end at 720p", decode_dc_image(decode_dc(data)), 271828)
    except DecodeError as exc:
        FAILURES.append(f"end to end at 720p failed: {exc}")

    for line in FAILURES:
        print(f"  FAIL  {line}")
    if not FAILURES:
        print("  ok    bar pattern: geometry, tolerance, refusals, JPEG round trip")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
