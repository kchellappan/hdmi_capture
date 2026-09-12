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

# Two more at the end, mirroring the leading pair: black then white. These make the
# pattern self-locating, which is what lets it be read out of a window of any size,
# anywhere on the display.
#
# Without them the reader has to assume the band spans the viewport exactly, which in turn
# forces the page to be fullscreen -- and getting a window fullscreen on a chosen monitor
# is the least portable thing in this whole repo. Measured on GNOME/Wayland, Chrome's
# --kiosk did not take effect and the window covered 50-88% of the display; the pattern
# was captured perfectly and decoded to nothing, because the reader was sampling desktop
# background.
#
# Because the bar before each is always the opposite colour, the first and last runs of
# bright columns are each exactly one bar wide. That gives the left edge, the right edge
# and the pitch, with no assumption about window geometry or scaling.
TAIL_BARS = 2

TOTAL_BARS = SYNC_BARS + DATA_BITS + CHECK_BITS + TAIL_BARS

# Fraction of the drawing surface left as dark margin on each side of the band. Without
# it the leading white bar touches the window edge, and anything bright behind the window
# -- pale wallpaper, another window -- merges with it and moves the located left edge.
# The reader recovers from that (see the inner-edge derivation in decode_columns), but it
# costs a candidate geometry and there is no reason to make it work harder.
RENDER_MARGIN = 0.05

# Fraction of a bar's width sampled at its centre. Bar edges blur through scaling and
# compression, so the margins are discarded rather than averaged in.
SAMPLE_FRACTION = 0.5

# How far a bar's sampled level may sit from the nearer reference level, as a fraction of
# the gap between them. Generous enough for JPEG ringing and a scaled display, tight
# enough that an alignment landing on bar boundaries is rejected.
BIMODAL_TOLERANCE = 0.30

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
    """The bars for a 20-bit value: white, black, data, checksum, black, white.

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
    bars += [False, True]
    return bars


def bar_centre_fraction(index: int) -> float:
    """Horizontal centre of bar `index`, as a fraction of screen width."""
    return (index + 0.5) * BAR_WIDTH


class DecodeError(ValueError):
    """The pattern could not be read out of this frame."""


def bright_runs(columns: list[int], threshold: float) -> list[tuple[int, int]]:
    """Contiguous spans of columns above `threshold`, as (start, end_exclusive)."""
    runs: list[tuple[int, int]] = []
    start = None
    for x, value in enumerate(columns):
        if value > threshold:
            if start is None:
                start = x
        elif start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, len(columns)))
    return runs


def sample_bars(columns: list[int], left: float, pitch: float) -> list[float]:
    """Mean level of each bar, given where the pattern starts and how wide a bar is.

    Split out of the decode so a caller that failed can report what it actually saw. A
    latency tool whose only output is "could not read the pattern" leaves someone staring
    at a display with no way to tell what went wrong.
    """
    out: list[float] = []
    half = pitch * SAMPLE_FRACTION / 2.0
    for bar in range(TOTAL_BARS):
        centre = left + (bar + 0.5) * pitch
        lo = max(0, int(round(centre - half)))
        hi = min(len(columns), max(lo + 1, int(round(centre + half))))
        out.append(sum(columns[lo:hi]) / (hi - lo))
    return out


def _decode_at(columns: list[int], left: float, pitch: float) -> int:
    """Decode assuming the pattern starts at `left` with bars `pitch` columns wide."""
    samples = sample_bars(columns, left, pitch)
    white, black = samples[0], samples[1]
    if white - black < MIN_CONTRAST * 255:
        raise DecodeError(
            f"contrast too low to read: reference bars came back {black:.0f} and "
            f"{white:.0f}. The pattern may not be on screen, or the display is off")

    # Every sample must sit close to one reference level or the other.
    #
    # This is what makes it safe to search for the geometry at all. A wrong alignment
    # samples across bar boundaries and comes back mid-range, while a correct one lands
    # inside flat bars. Without this check the only validation is the checksum and the
    # trailing pair -- six bits, against roughly a hundred candidate alignments, so wrong
    # geometries passed routinely and returned confident nonsense.
    margin = white - black
    for level in samples:
        if min(abs(level - white), abs(level - black)) > BIMODAL_TOLERANCE * margin:
            raise DecodeError(
                f"a bar sampled {level:.0f}, between the {black:.0f}/{white:.0f} "
                f"references -- the alignment is wrong or the image is blurred")

    threshold = (white + black) / 2.0
    bits = [level > threshold for level in samples[SYNC_BARS:]]
    # The trailing pair must be black then white, or this is not the pattern.
    if bits[-2] or not bits[-1]:
        raise DecodeError("trailing reference bars are not black-then-white")

    value = 0
    for bit in bits[:DATA_BITS]:
        value = (value << 1) | int(bit)
    check = 0
    for bit in bits[DATA_BITS:DATA_BITS + CHECK_BITS]:
        check = (check << 1) | int(bit)
    if check != checksum(value):
        raise DecodeError("checksum mismatch: a bar was misread")
    return value


# A bar narrower than this cannot be sampled reliably once the capture has been through
# JPEG and DC reduction: the sampled window collapses to a single column and bleeds from
# its neighbours.
MIN_PITCH_COLUMNS = 1.5


def locate(columns: list[int]) -> tuple[float, float]:
    """Where the pattern sits in this row, as (left edge, bar pitch), in columns.

    The first bar is white and the second always black, so the first run of bright
    columns is exactly one bar. The same holds at the other end in reverse. Taking the
    outermost pair gives the full extent, and dividing by the bar count gives a pitch that
    is fractional -- which matters, because rounding it accumulates: at 3.5 columns per
    bar an integer pitch walks a whole bar off alignment by the far end of the pattern.

    Raises DecodeError if no plausible geometry is found.
    """
    lowest, highest = min(columns), max(columns)
    if highest - lowest < MIN_CONTRAST * 255:
        raise DecodeError(
            f"contrast too low to read: this row ranges {lowest} to {highest}. The "
            f"pattern may not be on screen, or the display is off")
    runs = bright_runs(columns, (lowest + highest) / 2.0)
    if len(runs) < 2:
        raise DecodeError(f"found {len(runs)} bright run(s); the pattern needs at least 2")
    left = runs[0][0]
    right = runs[-1][1]
    pitch = (right - left) / TOTAL_BARS
    if pitch < MIN_PITCH_COLUMNS:
        raise DecodeError(
            f"pattern spans only {right - left} columns, {pitch:.1f} per bar -- too "
            f"small to read. Make the window bigger, or capture at a higher resolution")
    return float(left), pitch


def decode_columns(columns: list[int]) -> int:
    """Recover the value from one row of column means.

    The pattern is located rather than assumed, so the window it was drawn in can be any
    size and anywhere on the display.

    On how much this can be trusted, measured rather than asserted. Against corrupted
    patterns (one to three bars flipped), 9.6% still decode to *something* with only the
    checksum and the trailing pair; the bimodality check takes that to 6.7%. That floor is
    the four-bit checksum's one-in-sixteen collision rate and no amount of care here will
    beat it.

    What makes the measurement sound is the layer above: a caller that checks the decoded
    timestamp against the frame's own. A wrong decode lands anywhere in the 17-minute
    window, so the chance it also looks like a plausible latency is about one in a
    thousand -- taking the whole path to roughly 0.007% per frame, and a median over a
    hundred samples absorbs what survives. tools/vcap-glass-to-glass does that check;
    anything else reading this pattern must do it too.
    """
    if len(columns) < TOTAL_BARS * 2:
        raise DecodeError(f"only {len(columns)} columns; too few to resolve "
                          f"{TOTAL_BARS} bars")
    lowest, highest = min(columns), max(columns)
    if highest - lowest < MIN_CONTRAST * 255:
        raise DecodeError(
            f"contrast too low to read: this row ranges {lowest} to {highest}. The "
            f"pattern may not be on screen, or the display is off")

    runs = bright_runs(columns, (lowest + highest) / 2.0)
    if len(runs) < 2:
        raise DecodeError(f"found {len(runs)} bright run(s); the pattern needs at least 2")

    # The pattern's extent is bounded by the FIRST and LAST bright runs -- no other pair
    # can be right -- so only those two are considered. The remaining question is whether
    # either edge has bled into something bright beside the window, and there are exactly
    # four combinations of that.
    #
    # Searching more widely was tried and is actively harmful. Every pair of runs gives a
    # candidate geometry, roughly a hundred of them, against six bits of validation
    # (checksum plus the trailing pair) -- so wrong alignments passed routinely and
    # returned confident nonsense. Measured: with the wide search, every single-bar
    # corruption still "decoded", just to a different value.
    #
    # An outer edge is the outside of a run; an inner edge is the boundary that a run
    # shares with the black bar next to it, which survives bleeding because bar 1 and the
    # second-to-last bar are always black.
    first, last = runs[0], runs[-1]
    spans = (
        (first[0], last[1], TOTAL_BARS, False),       # both edges clean
        (first[1], last[0], TOTAL_BARS - 2, True),    # both bled
        (first[0], last[0], TOTAL_BARS - 1, False),   # right edge bled
        (first[1], last[1], TOTAL_BARS - 1, True),    # left edge bled
    )

    last_error = "no candidate geometry decoded"
    widest = 0.0
    for left_edge, right_edge, bars_between, left_is_inner in spans:
        if bars_between <= 0 or right_edge <= left_edge:
            continue
        pitch = (right_edge - left_edge) / bars_between
        if pitch < MIN_PITCH_COLUMNS:
            # Report the widest attempt: a pattern that is merely small should say so,
            # rather than quoting a near-zero pitch from a degenerate candidate.
            widest = max(widest, pitch)
            last_error = (
                f"pattern spans about {widest * TOTAL_BARS:.0f} columns, "
                f"{widest:.1f} per bar -- too small to read. Make the window bigger, "
                f"or capture at a higher resolution")
            continue
        left = left_edge - pitch if left_is_inner else float(left_edge)
        try:
            return _decode_at(columns, left, pitch)
        except DecodeError as exc:
            last_error = str(exc)
    raise DecodeError(last_error)


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
