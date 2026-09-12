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

from vcap.barcode import (BAR_WIDTH, BAND_HEIGHT, BAND_TOP, DATA_BITS, RENDER_MARGIN,
                          SYNC_BARS,
                          TOTAL_BARS, WRAP_MS, DecodeError, band_columns, checksum,
                          decode_columns, decode_dc_image, encode, locate,
                          reconstruct_ms, sample_bars)
from vcap.jpeg_dc import decode_dc

FAILURES: list[str] = []


def check(name: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{name}: got {got!r}, want {want!r}")


def render_bars(bars: list[bool], width: int = 240, white: int = 235, black: int = 18,
                noise: int = 0, background: int = 24) -> list[int]:
    """Synthetic column means for a pattern drawn across the full width.

    The background is dark, matching what the renderer actually draws. An earlier version
    used mid-gray 128, which sits within a rounding error of the black/white midpoint --
    so it read as bright, merged with the trailing reference bar, and pushed the located
    right edge to the frame boundary. That is a property of the test fixture, not of a
    real capture, and it made a correct decoder look broken.
    """
    out = []
    for x in range(width):
        index = int(((x + 0.5) / width) / BAR_WIDTH)
        level = (white if bars[index] else black) if index < TOTAL_BARS else background
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

    # The pattern is located, not assumed, so a window of any size anywhere on the
    # display decodes. This is the property that removes the fullscreen requirement --
    # and it is not hypothetical: on GNOME/Wayland, Chrome's --kiosk did not take, the
    # window covered 50-88% of the display, and the old fixed-geometry reader saw only
    # desktop background.
    def placed(value, width, x0, span, white=235, black=18, background=32):
        bars = encode(value)
        out = []
        for x in range(width):
            f = (x + 0.5) / width
            if f < x0 or f >= x0 + span:
                out.append(background)
                continue
            index = min(int((f - x0) / span * TOTAL_BARS), TOTAL_BARS - 1)
            out.append(white if bars[index] else black)
        return out

    for name, args in (
        ("spanning the frame", (240, 0.0, 1.0)),
        ("in the right half", (240, 0.5, 0.38)),
        ("the geometry Chrome actually produced", (240, 0.504, 0.379)),
        ("a quarter of the frame", (240, 0.2, 0.25)),
        ("at 720p capture width", (160, 0.1, 0.6)),
    ):
        try:
            check(f"decodes {name}", decode_columns(placed(424242, *args)), 424242)
        except DecodeError as exc:
            FAILURES.append(f"decoding {name} raised: {exc}")

    # Light wallpaper touching the window merges with the leading white bar, putting the
    # outer left edge inside the wallpaper. The inner edges of the first and last bright
    # runs are real bar boundaries regardless, which is what recovers this.
    bars = encode(555)
    bleed = []
    for x in range(240):
        f = (x + 0.5) / 240
        if f < 0.3 or f >= 0.8:
            bleed.append(250)
        else:
            bleed.append(235 if bars[min(int((f - 0.3) / 0.5 * TOTAL_BARS),
                                         TOTAL_BARS - 1)] else 18)
    try:
        check("decodes with white wallpaper bleeding into the first bar",
              decode_columns(bleed), 555)
    except DecodeError as exc:
        FAILURES.append(f"wallpaper case raised: {exc}")

    # A pattern too small to sample must say so, rather than returning a wrong value.
    try:
        decode_columns(placed(1, 240, 0.2, 0.12))
        FAILURES.append("an unreadably small pattern was decoded")
    except DecodeError as exc:
        if "too small" not in str(exc):
            FAILURES.append(f"small pattern rejected unhelpfully: {exc}")

    # The renderer's layout contract, checked without a toolkit.
    #
    # vcap-glass-to-glass draws the band inset by RENDER_MARGIN inside a window that can
    # be any size, anywhere on the display. This reproduces that geometry exactly and
    # feeds it through the reader, so the two cannot drift apart -- the part of the loop
    # that tkinter would otherwise be needed to exercise.
    def as_rendered(value, frame_width, window_x, window_width, background=24):
        """Column means for a window drawn at window_x, as the card would capture it."""
        bars = encode(value)
        left = window_x + window_width * RENDER_MARGIN
        pitch = window_width * (1.0 - 2 * RENDER_MARGIN) / TOTAL_BARS
        out = []
        for x in range(frame_width):
            index = int((x + 0.5 - left) // pitch)
            if 0 <= index < TOTAL_BARS:
                out.append(235 if bars[index] else 18)
            else:
                out.append(background)
        return out

    for name, (frame_w, win_x, win_w) in (
        ("filling the display", (240, 0, 240)),
        ("a window half the display", (240, 60, 120)),
        ("where Chrome actually put it", (240, 121, 91)),
        ("hard against the left edge", (240, 0, 140)),
        ("at 720p capture width", (160, 20, 120)),
    ):
        try:
            check(f"the rendered layout decodes {name}",
                  decode_columns(as_rendered(861234, frame_w, win_x, win_w)), 861234)
        except DecodeError as exc:
            FAILURES.append(f"rendered layout {name} raised: {exc}")

    # How often a corrupted pattern still decodes to something. Measured rather than
    # asserted, with a fixed seed so the number is stable.
    #
    # This exists because the validation is layered and each layer was easy to add without
    # evidence it helped. Removing the bimodality check takes this from 6.7% to 9.6%,
    # which is what justifies keeping it -- and the 6.7% floor is the four-bit checksum's
    # collision rate, which is why the tool must also check the decoded timestamp against
    # the frame's own. Neither fact was obvious before it was counted.
    import random as _random
    rng = _random.Random(7)
    decoded = 0
    trials = 1500
    for _ in range(trials):
        corrupted = encode(rng.getrandbits(DATA_BITS))
        for _ in range(rng.randint(1, 3)):
            k = rng.randrange(TOTAL_BARS)
            corrupted[k] = not corrupted[k]
        try:
            decode_columns(render_bars(corrupted))
            decoded += 1
        except DecodeError:
            pass
    rate = 100.0 * decoded / trials
    if rate > 8.0:
        FAILURES.append(f"corrupted patterns decode {rate:.1f}% of the time, over the 8% "
                        f"budget -- a validation layer has been weakened")

    # The diagnostic surface, load-bearing because it is the only output of a failed run.
    columns = render_columns(424242, white=190, black=70)
    left, pitch = locate(columns)
    bars_seen = sample_bars(columns, left, pitch)
    check("sample_bars returns one level per bar", len(bars_seen), TOTAL_BARS)
    check("it reports the white reference it actually saw", round(bars_seen[0]), 190)
    check("and the black one", round(bars_seen[1]), 70)

    data = fake_jpeg.encode_bars(encode(555), 240, 135, BAR_WIDTH, BAND_TOP, BAND_HEIGHT)
    image = decode_dc(data)
    extracted = band_columns(image)
    check("band_columns spans the full frame width", len(extracted), image.width)
    check("and the band it picks decodes", decode_columns(extracted), 555)

    for line in FAILURES:
        print(f"  FAIL  {line}")
    if not FAILURES:
        print("  ok    bar pattern: geometry, tolerance, refusals, JPEG round trip")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
