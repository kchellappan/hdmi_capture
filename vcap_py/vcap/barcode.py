"""A bar pattern that survives an HDMI round trip, and the code to read it back.

Used by tools/vcap-glass-to-glass to carry a timestamp from a page on the screen, through
the display, the cable and the capture card, back into a captured frame.

The design constraint is unusual: the pattern has to be legible after being rendered by a
browser, scanned out by a GPU, compressed to MJPEG by a cheap capture card, and then read
back from DC coefficients alone -- an eighth-scale, luma-only image. So it uses very few,
very large, black-and-white bars, and it carries its own reference levels rather than
assuming what black and white come back as.

Everything is expressed as a fraction of the display's width and height, so the pattern
decodes at whatever resolution the card happens to deliver. Nothing here assumes the
capture geometry matches the display's.
"""
from __future__ import annotations

# The pattern is a band across the top of the screen. Top, because a browser in fullscreen
# puts nothing there, and because the first rows of a frame are the earliest part of a
# progressive scanout -- so a timestamp read from them is as close to the display's own
# clock as this method can get.
BAND_TOP = 0.0
BAND_HEIGHT = 0.125          # of screen height
BAR_WIDTH = 1.0 / 32.0       # of screen width

# Two reference bars come first: one full white, one full black. They are not decoration.
# A capture card applies its own gain and the JPEG quantiser moves levels around, so a
# fixed threshold would be wrong on any display but the one it was tuned on. Reading the
# actual extremes out of the same frame makes the threshold self-calibrating.
SYNC_BARS = 2

DATA_BITS = 20               # low bits of a millisecond timestamp: wraps every ~17 min
CHECK_BITS = 4               # XOR of the five data nibbles
TOTAL_BARS = SYNC_BARS + DATA_BITS + CHECK_BITS

# Fraction of a bar's width sampled at its centre. Bar edges blur through scaling and
# compression, so the margins are discarded rather than averaged in.
SAMPLE_FRACTION = 0.5

# A decode is rejected when white and black are closer than this, as a fraction of the
# full range. Below it the frame is too washed out, too dark, or not showing the pattern
# at all -- and a threshold derived from noise would return a plausible wrong number.
MIN_CONTRAST = 0.15


def checksum(value: int) -> int:
    """XOR of the five nibbles of a 20-bit value.

    Cheap, and it catches the failure that matters: a single misread bar. Without it a
    flipped bit yields a timestamp that is wrong by a power of two milliseconds and
    entirely plausible, which would corrupt a latency measurement rather than fail it.
    """
    check = 0
    for shift in range(0, DATA_BITS, 4):
        check ^= (value >> shift) & 0xF
    return check & 0xF


def encode(value: int) -> list[bool]:
    """The bars for a 20-bit value: white, black, then data, then checksum.

    Bits are most-significant first, left to right, which is the order a person reading
    the screen would expect if they ever had to check one by hand.
    """
    if not 0 <= value < (1 << DATA_BITS):
        raise ValueError(f"value must fit in {DATA_BITS} bits")
    bars = [True, False]
    for i in range(DATA_BITS - 1, -1, -1):
        bars.append(bool((value >> i) & 1))
    check = checksum(value)
    for i in range(CHECK_BITS - 1, -1, -1):
        bars.append(bool((check >> i) & 1))
    return bars


def bar_centre_fraction(index: int) -> float:
    """Horizontal centre of bar `index`, as a fraction of screen width."""
    return (index + 0.5) * BAR_WIDTH


class DecodeError(ValueError):
    """The pattern could not be read out of this frame."""


def sample_bars(columns: list[int]) -> list[float]:
    """Mean level of each bar, before any thresholding.

    Split out of decode_columns so a caller that failed to decode can report what it
    actually saw. A latency tool whose only output is "could not read the pattern" leaves
    someone staring at a display with no way to tell whether the page is in the wrong
    place, the band is off screen, or the contrast is simply too low -- and that is the
    normal first experience of a physical setup.
    """
    width = len(columns)
    out: list[float] = []
    for bar in range(TOTAL_BARS):
        centre = bar_centre_fraction(bar) * width
        half = BAR_WIDTH * width * SAMPLE_FRACTION / 2.0
        lo = max(0, int(round(centre - half)))
        hi = min(width, max(lo + 1, int(round(centre + half))))
        out.append(sum(columns[lo:hi]) / (hi - lo))
    return out


def decode_columns(columns: list[int]) -> int:
    """Recover the value from one row of column means spanning the full frame width.

    Takes column means rather than an image so that the geometry logic is testable
    without constructing one, and so the caller decides which rows to average.
    """
    if len(columns) < TOTAL_BARS * 2:
        raise DecodeError(f"only {len(columns)} columns; too few to resolve "
                          f"{TOTAL_BARS} bars")

    samples = sample_bars(columns)
    white, black = samples[0], samples[1]
    if white - black < MIN_CONTRAST * 255:
        raise DecodeError(
            f"contrast too low to read: reference bars came back {black:.0f} and "
            f"{white:.0f}. The pattern may not be on screen, or the display is off")

    threshold = (white + black) / 2.0
    bits = [s > threshold for s in samples[SYNC_BARS:]]

    value = 0
    for bit in bits[:DATA_BITS]:
        value = (value << 1) | int(bit)
    check = 0
    for bit in bits[DATA_BITS:DATA_BITS + CHECK_BITS]:
        check = (check << 1) | int(bit)

    if check != checksum(value):
        raise DecodeError("checksum mismatch: a bar was misread")
    return value


def decode_dc_image(image) -> int:
    """Read the pattern out of a vcap.jpeg_dc.DcImage.

    Averages the middle of the band rather than all of it: the top edge catches whatever
    the browser draws at the very top of the viewport, and the bottom edge blurs into the
    page content below.
    """
    return decode_columns(band_columns(image))


# The pattern carries only the low bits of a millisecond clock, so a reader has to rebuild
# the rest. This lives here rather than in the tool that uses it because it is part of the
# encoding -- and because a tool script cannot be imported by a test.
WRAP_MS = 1 << DATA_BITS


def reconstruct_ms(encoded: int, reference_ms: float) -> float:
    """Rebuild a full millisecond timestamp from its low bits and a nearby reference.

    `reference_ms` is the capturing frame's own timestamp. The encoded value wraps every
    2**20 ms, about 17.5 minutes, while the interval being measured is tens of
    milliseconds -- so the right window is whichever candidate sits nearest the reference,
    unambiguous by a factor of thousands.

    Checking the neighbouring windows as well as the current one is what makes this
    correct across a wrap: without it, a frame captured just after the counter rolls over
    reconstructs to a timestamp 17 minutes in the future and yields a wildly negative
    latency.
    """
    base = int(reference_ms) // WRAP_MS * WRAP_MS
    candidates = (base + encoded - WRAP_MS, base + encoded, base + encoded + WRAP_MS)
    return float(min(candidates, key=lambda c: abs(reference_ms - c)))


def band_columns(image) -> list[int]:
    """The column means decode_dc_image works from. Exposed for diagnostics."""
    band_top = int(image.height * BAND_TOP)
    band_bottom = int(image.height * (BAND_TOP + BAND_HEIGHT))
    inset = max(1, (band_bottom - band_top) // 4)
    return image.column_means(band_top + inset, band_bottom - inset)
