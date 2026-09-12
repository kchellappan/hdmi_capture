"""A minimal baseline JPEG encoder, for testing the DC decoder against known values.

Encoding a whole JPEG would be a project. Encoding one whose DC coefficients are exactly
what you asked for is about a hundred lines, and it is a far better test input than a
captured frame: a real frame can only be checked for plausibility, while a synthetic one
has a right answer for every block.

That distinction mattered here. The first attempt at a fixture was a frame captured from
the card with nothing on its HDMI input, which decodes to a uniform image -- so it would
have passed just as happily against a decoder that returned a constant.

Grayscale, one component, no subsampling, all AC coefficients zero. The quantisation table
is all ones, so a block's stored DC value passes through unscaled and the decoder's
output is exactly predictable.
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "vcap_py"))

# The standard JPEG luminance DC table. Any canonical table would do; this one is what
# real encoders emit, so the decoder is exercised against codes it will actually meet.
DC_BITS = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
DC_VALS = list(range(12))

# A one-symbol AC table: end-of-block as a single zero bit. Every block here has zero AC
# coefficients, so nothing else is ever needed.
AC_BITS = [1] + [0] * 15
AC_VALS = [0x00]


def _canonical(counts: list[int], symbols: list[int]) -> dict[int, tuple[int, int]]:
    """symbol -> (code, length), assigned the way the JPEG spec does."""
    table: dict[int, tuple[int, int]] = {}
    code = 0
    k = 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            table[symbols[k]] = (code, length)
            code += 1
            k += 1
        code <<= 1
    return table


class _BitWriter:
    def __init__(self):
        self.out = bytearray()
        self.bits = 0
        self.count = 0

    def write(self, code: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            self.bits = (self.bits << 1) | ((code >> i) & 1)
            self.count += 1
            if self.count == 8:
                self._emit(self.bits & 0xFF)
                self.bits = 0
                self.count = 0

    def _emit(self, byte: int) -> None:
        self.out.append(byte)
        # Byte stuffing: a literal 0xFF inside a scan is written as 0xFF 0x00, so that a
        # reader can tell payload from a marker. The decoder undoes this; if either side
        # forgets, the stream desyncs a few blocks later rather than at the mistake.
        if byte == 0xFF:
            self.out.append(0x00)

    def flush(self) -> None:
        # Pad with ones, as the spec requires: a run of zeros could be read as a valid
        # code and produce one extra phantom block.
        while self.count:
            self.write(1, 1)


def _segment(code: int, payload: bytes) -> bytes:
    return bytes([0xFF, code]) + (len(payload) + 2).to_bytes(2, "big") + payload


def encode_dc_only(values: list[list[int]]) -> bytes:
    """A baseline JPEG whose 8x8 blocks decode to exactly `values`.

    `values` is a grid of target luma levels, 0-255, one per block: values[row][col]. The
    resulting image is 8*cols by 8*rows.
    """
    rows = len(values)
    cols = len(values[0])
    for row in values:
        if len(row) != cols:
            raise ValueError("every row must be the same length")

    dc_codes = _canonical(DC_BITS, DC_VALS)
    ac_codes = _canonical(AC_BITS, AC_VALS)

    out = bytearray(b"\xff\xd8")
    # Quantisation table: all ones, so a stored coefficient is its own value.
    out += _segment(0xDB, bytes([0x00]) + bytes([1] * 64))
    out += _segment(0xC0, bytes([8]) + (rows * 8).to_bytes(2, "big") +
                    (cols * 8).to_bytes(2, "big") + bytes([1, 0x01, 0x11, 0x00]))
    out += _segment(0xC4, bytes([0x00]) + bytes(DC_BITS) + bytes(DC_VALS))
    out += _segment(0xC4, bytes([0x10]) + bytes(AC_BITS) + bytes(AC_VALS))
    out += _segment(0xDA, bytes([1, 0x01, 0x00, 0x00, 0x3F, 0x00]))

    writer = _BitWriter()
    previous = 0
    for row in values:
        for target in row:
            # The decoder computes (dc * q) // 8 + 128 with q = 1, so a target level t
            # needs a stored coefficient of 8 * (t - 128). That division is exact, which
            # is what makes the expected output a single value rather than a range.
            dc = 8 * (int(target) - 128)
            diff = dc - previous
            previous = dc
            size = abs(diff).bit_length()
            code, length = dc_codes[size]
            writer.write(code, length)
            if size:
                # JPEG's sign convention: negative values are stored offset into the
                # bottom half of the range.
                writer.write(diff if diff > 0 else diff + (1 << size) - 1, size)
            code, length = ac_codes[0x00]
            writer.write(code, length)     # end of block: every AC coefficient is zero
    writer.flush()

    out += writer.out
    out += b"\xff\xd9"
    return bytes(out)


def encode_bars(bars: list[bool], width_blocks: int, height_blocks: int,
                bar_width_fraction: float, band_top: float, band_height: float,
                white: int = 235, black: int = 20, background: int = 128) -> bytes:
    """A JPEG containing a bar pattern, laid out the way vcap.barcode expects to find it.

    Used to test the decode path end to end without a display, a card, or a cable.
    """
    grid = [[background] * width_blocks for _ in range(height_blocks)]
    top = int(height_blocks * band_top)
    bottom = max(top + 1, int(height_blocks * (band_top + band_height)))
    for y in range(top, min(bottom, height_blocks)):
        for x in range(width_blocks):
            index = int(((x + 0.5) / width_blocks) / bar_width_fraction)
            if index < len(bars):
                grid[y][x] = white if bars[index] else black
    return encode_dc_only(grid)
