"""A baseline JPEG decoder that recovers only DC coefficients.

Decoding a full JPEG in pure Python would be slow and pointless -- numpy, Pillow and
OpenCV already do it properly, which is why vcap/decode.py exists. But there is one job
that needs pixels and must not need an install: measuring glass-to-glass latency, which
reads a high-contrast pattern back out of a captured frame. A capture box that cannot run
that measurement because it lacks numpy is a capture box that ships with an unmeasured
timestamp offset.

So this decodes the cheap part. A baseline JPEG's DC coefficient is the average of its
8x8 block, so Huffman-decoding the DC term of every block and discarding the AC terms
yields the image at one eighth scale, in luma only -- 240x135 from a 1080p frame. That is
ample for a pattern of large bars and costs no dependencies.

What it deliberately does not do: inverse DCT, chroma, upsampling, colour conversion,
progressive scans, or arithmetic coding. It is not a substitute for a real decoder and
must not grow into one. If something here needs a real image, it belongs behind
vcap/decode.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Markers that carry a length field and that this decoder reads.
_SOI, _EOI, _SOS, _DQT, _DHT, _DRI = 0xD8, 0xD9, 0xDA, 0xDB, 0xC4, 0xDD
# Baseline sequential, Huffman. The other SOFn markers are progressive, lossless or
# arithmetic-coded, none of which this handles -- and all of which it refuses by name
# rather than producing a plausible wrong image.
_SOF0, _SOF1 = 0xC0, 0xC1
_SOF_UNSUPPORTED = {
    0xC2: "progressive", 0xC3: "lossless", 0xC5: "differential sequential",
    0xC6: "differential progressive", 0xC7: "differential lossless",
    0xC9: "arithmetic sequential", 0xCA: "arithmetic progressive",
    0xCB: "arithmetic lossless", 0xCD: "differential arithmetic sequential",
    0xCE: "differential arithmetic progressive", 0xCF: "differential arithmetic lossless",
}


class JpegError(ValueError):
    """The data is not a baseline JPEG this decoder can read."""


@dataclass
class _Component:
    identifier: int
    h: int
    v: int
    quant_id: int
    dc_table: int = 0
    ac_table: int = 0


@dataclass
class _HuffTable:
    """A canonical Huffman table, as a dict keyed by (length, code).

    A flat dict is not the fastest representation -- a real decoder uses a lookup table
    indexed by the next N bits -- but it is short, obviously correct, and this decoder
    runs over a handful of frames during a calibration, not over a dataset.
    """
    lookup: dict = field(default_factory=dict)
    max_length: int = 0

    @classmethod
    def build(cls, counts: list[int], symbols: list[int]) -> "_HuffTable":
        table = cls()
        code = 0
        k = 0
        for length in range(1, 17):
            for _ in range(counts[length - 1]):
                if k >= len(symbols):
                    raise JpegError("Huffman table declares more codes than symbols")
                table.lookup[(length, code)] = symbols[k]
                code += 1
                k += 1
            code <<= 1
            if counts[length - 1]:
                table.max_length = length
        return table


class _BitReader:
    """Reads the entropy-coded segment, undoing JPEG's byte stuffing.

    Inside a scan, a literal 0xFF is written as 0xFF 0x00, so the zero has to be swallowed.
    Any other 0xFF pair is a marker and ends the scan -- which for this decoder means a
    restart marker or the end of the image.
    """

    def __init__(self, data: bytes, start: int):
        self.data = data
        self.pos = start
        self.bits = 0
        self.count = 0

    def _next_byte(self) -> int | None:
        if self.pos >= len(self.data):
            return None
        byte = self.data[self.pos]
        self.pos += 1
        if byte != 0xFF:
            return byte
        if self.pos >= len(self.data):
            return None
        following = self.data[self.pos]
        if following == 0x00:
            self.pos += 1
            return 0xFF
        return None                      # a real marker; the scan stops here

    def read_bit(self) -> int:
        if self.count == 0:
            byte = self._next_byte()
            if byte is None:
                # Running off the end of a truncated scan is expected: this repo's card
                # emits a headless fragment as the first frame of nearly every stream.
                # Feeding zeros lets the caller finish and check the result rather than
                # unwinding through an exception per frame.
                self.bits, self.count = 0, 8
            else:
                self.bits, self.count = byte, 8
        self.count -= 1
        return (self.bits >> self.count) & 1

    def read_bits(self, n: int) -> int:
        value = 0
        for _ in range(n):
            value = (value << 1) | self.read_bit()
        return value

    def align(self) -> None:
        self.count = 0

    def skip_restart_marker(self) -> bool:
        """Step over an RSTn marker if the stream is sitting on one."""
        self.align()
        while self.pos + 1 < len(self.data) and self.data[self.pos] == 0xFF:
            marker = self.data[self.pos + 1]
            if 0xD0 <= marker <= 0xD7:
                self.pos += 2
                return True
            return False
        return False

    def decode(self, table: _HuffTable) -> int:
        code = 0
        for length in range(1, 17):
            code = (code << 1) | self.read_bit()
            symbol = table.lookup.get((length, code))
            if symbol is not None:
                return symbol
        raise JpegError("no Huffman code matched in 16 bits; the scan is not baseline")


def _extend(value: int, size: int) -> int:
    """JPEG's sign convention: the top half of the range is positive, the bottom negative."""
    if size == 0:
        return 0
    return value if value >= (1 << (size - 1)) else value - (1 << size) + 1


@dataclass
class DcImage:
    """Luma at one eighth scale: one sample per 8x8 block, row-major, 0-255."""
    width: int                       # in blocks
    height: int                      # in blocks
    samples: list[int]
    full_width: int                  # the source frame's pixel dimensions
    full_height: int

    def at(self, x: int, y: int) -> int:
        return self.samples[y * self.width + x]

    def row(self, y: int) -> list[int]:
        return self.samples[y * self.width:(y + 1) * self.width]

    def column_means(self, y0: int, y1: int) -> list[int]:
        """Mean of each column over rows [y0, y1). The primitive a bar pattern needs."""
        y0 = max(0, min(self.height, y0))
        y1 = max(y0 + 1, min(self.height, y1))
        rows = y1 - y0
        out = []
        for x in range(self.width):
            total = 0
            for y in range(y0, y1):
                total += self.samples[y * self.width + x]
            out.append(total // rows)
        return out


def decode_dc(data: bytes) -> DcImage:
    """Decode the DC coefficients of a baseline JPEG into an eighth-scale luma image.

    Raises JpegError for anything it cannot read, naming what it found -- a progressive
    scan produces a specific complaint rather than a plausible wrong image.
    """
    if len(data) < 4 or data[0] != 0xFF or data[1] != _SOI:
        raise JpegError("not a JPEG: no start-of-image marker")

    quant: dict[int, list[int]] = {}
    dc_tables: dict[int, _HuffTable] = {}
    ac_tables: dict[int, _HuffTable] = {}
    components: list[_Component] = []
    frame_width = frame_height = 0
    restart_interval = 0

    i = 2
    while i + 3 < len(data):
        if data[i] != 0xFF:
            raise JpegError(f"expected a marker at byte {i}")
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in _SOF_UNSUPPORTED:
            raise JpegError(f"{_SOF_UNSUPPORTED[marker]} JPEG; this decoder reads "
                            f"baseline sequential only")
        length = (data[i + 2] << 8) | data[i + 3]
        segment = data[i + 4:i + 2 + length]

        if marker in (_SOF0, _SOF1):
            if len(segment) < 6:
                raise JpegError("truncated start-of-frame")
            precision = segment[0]
            if precision != 8:
                raise JpegError(f"{precision}-bit samples; only 8-bit is supported")
            frame_height = (segment[1] << 8) | segment[2]
            frame_width = (segment[3] << 8) | segment[4]
            count = segment[5]
            for c in range(count):
                base = 6 + c * 3
                components.append(_Component(
                    identifier=segment[base],
                    h=segment[base + 1] >> 4,
                    v=segment[base + 1] & 0x0F,
                    quant_id=segment[base + 2]))
        elif marker == _DQT:
            pos = 0
            while pos < len(segment):
                precision, table_id = segment[pos] >> 4, segment[pos] & 0x0F
                pos += 1
                size = 64 * (2 if precision else 1)
                raw = segment[pos:pos + size]
                # Only the DC term is ever used, and it is the first in zigzag order.
                quant[table_id] = ([((raw[0] << 8) | raw[1])] if precision
                                   else [raw[0]])
                pos += size
        elif marker == _DHT:
            pos = 0
            while pos < len(segment):
                table_class, table_id = segment[pos] >> 4, segment[pos] & 0x0F
                counts = list(segment[pos + 1:pos + 17])
                total = sum(counts)
                symbols = list(segment[pos + 17:pos + 17 + total])
                table = _HuffTable.build(counts, symbols)
                (ac_tables if table_class else dc_tables)[table_id] = table
                pos += 17 + total
        elif marker == _DRI:
            restart_interval = (segment[0] << 8) | segment[1]
        elif marker == _SOS:
            count = segment[0]
            for c in range(count):
                identifier = segment[1 + c * 2]
                tables = segment[2 + c * 2]
                for comp in components:
                    if comp.identifier == identifier:
                        comp.dc_table = tables >> 4
                        comp.ac_table = tables & 0x0F
            return _decode_scan(data, i + 2 + length, components, quant,
                                dc_tables, ac_tables, frame_width, frame_height,
                                restart_interval)
        elif marker == _EOI:
            break
        i += 2 + length

    raise JpegError("no scan found; the frame is truncated before its image data")


def _decode_scan(data: bytes, start: int, components: list[_Component],
                 quant: dict[int, list[int]], dc_tables: dict[int, _HuffTable],
                 ac_tables: dict[int, _HuffTable], width: int, height: int,
                 restart_interval: int) -> DcImage:
    if not components or width == 0 or height == 0:
        raise JpegError("scan without a frame header")

    h_max = max(c.h for c in components)
    v_max = max(c.v for c in components)
    mcu_w, mcu_h = 8 * h_max, 8 * v_max
    mcus_x = (width + mcu_w - 1) // mcu_w
    mcus_y = (height + mcu_h - 1) // mcu_h

    luma = components[0]
    # Blocks of the luma plane, which is what gets returned. With 4:2:2 the luma has two
    # blocks per MCU horizontally, so the output is not simply one sample per MCU.
    out_w = mcus_x * luma.h
    out_h = mcus_y * luma.v
    samples = [0] * (out_w * out_h)

    reader = _BitReader(data, start)
    predictions = {c.identifier: 0 for c in components}
    since_restart = 0

    for my in range(mcus_y):
        for mx in range(mcus_x):
            if restart_interval and since_restart == restart_interval:
                if reader.skip_restart_marker():
                    predictions = {c.identifier: 0 for c in components}
                    since_restart = 0
            for comp in components:
                dc_table = dc_tables.get(comp.dc_table)
                ac_table = ac_tables.get(comp.ac_table)
                if dc_table is None or ac_table is None:
                    raise JpegError("scan references a Huffman table that was not defined")
                q = quant.get(comp.quant_id, [1])[0] or 1
                for by in range(comp.v):
                    for bx in range(comp.h):
                        size = reader.decode(dc_table)
                        diff = _extend(reader.read_bits(size), size) if size else 0
                        predictions[comp.identifier] += diff
                        if comp is luma:
                            # Dequantise, undo the level shift, and clamp. The DC term is
                            # the block mean scaled by 8, which is the /8 below.
                            value = (predictions[comp.identifier] * q) // 8 + 128
                            x = mx * comp.h + bx
                            y = my * comp.v + by
                            if x < out_w and y < out_h:
                                samples[y * out_w + x] = 0 if value < 0 else (
                                    255 if value > 255 else value)
                        _skip_ac(reader, ac_table)
            since_restart += 1

    return DcImage(width=out_w, height=out_h, samples=samples,
                   full_width=width, full_height=height)


def _skip_ac(reader: _BitReader, table: _HuffTable) -> None:
    """Consume a block's 63 AC coefficients without storing them."""
    k = 1
    while k < 64:
        symbol = reader.decode(table)
        run, size = symbol >> 4, symbol & 0x0F
        if size == 0:
            if run != 15:
                return                   # end of block
            k += 16                      # a run of sixteen zeros
            continue
        k += run + 1
        reader.read_bits(size)
