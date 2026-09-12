"""The V4L2 ioctl interface, in ctypes.

This exists so the rest of the package has no dependencies. V4L2 is ioctls on a character
device plus mmap; both are in the standard library, so binding it directly costs about
three hundred lines and buys the property that submoduling this repo adds nothing to
yours. See tests/check_stdlib_only.py.

Two things here are easy to get wrong and expensive to debug.

**Structure sizes are part of the ABI.** An ioctl request number encodes the size of its
argument, so a structure that is one byte off does not misbehave subtly -- the kernel
rejects the call with ENOTTY and it looks as though the device does not support the
operation at all. The padding below is therefore load-bearing, not defensive: it is
explained where it appears, and tests/test_ioctl_abi.py pins the resulting request
numbers against the documented constants.

**The timestamp is the reason this module exists.** Every dequeued buffer carries a
kernel timestamp that uvcvideo derives from the USB start-of-frame counter. That is the
closest thing to a capture instant that software can see, and it is on CLOCK_MONOTONIC --
the same clock rpi_gamepad_bridge's client stamps arrivals with. A timestamp taken in
userspace after a read() would fold in queueing and decode jitter instead. Nothing in
this package is allowed to discard it.
"""
from __future__ import annotations

import ctypes
import fcntl

# ---------------------------------------------------------------- ioctl request encoding

_IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS = 8, 8, 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2


def _ioc(direction: int, typ: str, nr: int, size: int) -> int:
    return ((direction << _IOC_DIRSHIFT) | (ord(typ) << _IOC_TYPESHIFT)
            | (nr << _IOC_NRSHIFT) | (size << _IOC_SIZESHIFT))


def _ior(nr: int, struct) -> int:
    return _ioc(_IOC_READ, "V", nr, ctypes.sizeof(struct))


def _iow(nr: int, struct) -> int:
    return _ioc(_IOC_WRITE, "V", nr, ctypes.sizeof(struct))


def _iowr(nr: int, struct) -> int:
    return _ioc(_IOC_READ | _IOC_WRITE, "V", nr, ctypes.sizeof(struct))


# ------------------------------------------------------------------------------ constants

BUF_TYPE_VIDEO_CAPTURE = 1
BUF_TYPE_META_CAPTURE = 13
MEMORY_MMAP = 1

CAP_VIDEO_CAPTURE = 0x00000001
CAP_META_CAPTURE = 0x00800000
CAP_STREAMING = 0x04000000

FMT_FLAG_COMPRESSED = 0x0001

FRMSIZE_TYPE_DISCRETE = 1
FRMIVAL_TYPE_DISCRETE = 1

# A buffer the driver marked as containing an error. uvcvideo sets this when a frame
# arrived with a payload error or was assembled from an incomplete set of USB packets.
# Such a buffer still has bytesused > 0, so it has to be tested for explicitly rather
# than inferred from a short read.
BUF_FLAG_ERROR = 0x00000040

# Which clock the timestamp is on, and what instant it names. uvcvideo reports MONOTONIC
# and, when the device's clock can be tracked, start-of-frame. Anything else means the
# timestamp is not comparable with time.monotonic_ns() and the caller needs to know.
BUF_FLAG_TIMESTAMP_MASK = 0x0000E000
BUF_FLAG_TIMESTAMP_UNKNOWN = 0x00000000
BUF_FLAG_TIMESTAMP_MONOTONIC = 0x00002000
BUF_FLAG_TIMESTAMP_COPY = 0x00004000
BUF_FLAG_TSTAMP_SRC_MASK = 0x00070000
BUF_FLAG_TSTAMP_SRC_EOF = 0x00000000
BUF_FLAG_TSTAMP_SRC_SOF = 0x00010000


def fourcc(value: int) -> str:
    """The four character code for a pixel format, as it appears in documentation."""
    return "".join(chr((value >> (8 * i)) & 0xFF) for i in range(4))


def fourcc_to_int(code: str) -> int:
    code = code.ljust(4)[:4]
    return sum(ord(c) << (8 * i) for i, c in enumerate(code))


# ----------------------------------------------------------------------------- structures


class Capability(ctypes.Structure):
    _fields_ = [
        ("driver", ctypes.c_char * 16),
        ("card", ctypes.c_char * 32),
        ("bus_info", ctypes.c_char * 32),
        ("version", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("device_caps", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


class FmtDesc(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("description", ctypes.c_char * 32),
        ("pixelformat", ctypes.c_uint32),
        ("mbus_code", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 3),
    ]


class _Discrete(ctypes.Structure):
    _fields_ = [("width", ctypes.c_uint32), ("height", ctypes.c_uint32)]


class _Stepwise(ctypes.Structure):
    _fields_ = [
        ("min_width", ctypes.c_uint32), ("max_width", ctypes.c_uint32),
        ("step_width", ctypes.c_uint32), ("min_height", ctypes.c_uint32),
        ("max_height", ctypes.c_uint32), ("step_height", ctypes.c_uint32),
    ]


class _FrmSizeUnion(ctypes.Union):
    _fields_ = [("discrete", _Discrete), ("stepwise", _Stepwise)]


class FrmSizeEnum(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("pixel_format", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("u", _FrmSizeUnion),
        ("reserved", ctypes.c_uint32 * 2),
    ]


class Fract(ctypes.Structure):
    _fields_ = [("numerator", ctypes.c_uint32), ("denominator", ctypes.c_uint32)]

    def as_hz(self) -> float:
        return self.denominator / self.numerator if self.numerator else 0.0


class _IvalStepwise(ctypes.Structure):
    _fields_ = [("min", Fract), ("max", Fract), ("step", Fract)]


class _FrmIvalUnion(ctypes.Union):
    _fields_ = [("discrete", Fract), ("stepwise", _IvalStepwise)]


class FrmIvalEnum(ctypes.Structure):
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("pixel_format", ctypes.c_uint32),
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("u", _FrmIvalUnion),
        ("reserved", ctypes.c_uint32 * 2),
    ]


class PixFormat(ctypes.Structure):
    _fields_ = [
        ("width", ctypes.c_uint32),
        ("height", ctypes.c_uint32),
        ("pixelformat", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("bytesperline", ctypes.c_uint32),
        ("sizeimage", ctypes.c_uint32),
        ("colorspace", ctypes.c_uint32),
        ("priv", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("enc", ctypes.c_uint32),
        ("quantization", ctypes.c_uint32),
        ("xfer_func", ctypes.c_uint32),
    ]


class Format(ctypes.Structure):
    """struct v4l2_format.

    The union in the kernel header runs to 200 bytes and contains struct v4l2_window,
    which holds a userspace pointer -- so on a 64-bit kernel the union is 8-byte aligned
    and four bytes of padding appear after `type`. Total size 208. Omitting that padding
    yields a 204-byte structure, a different request number, and ENOTTY from every
    S_FMT call.
    """
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("_pad", ctypes.c_uint32),
        ("pix", PixFormat),
        ("_raw_tail", ctypes.c_uint8 * (200 - ctypes.sizeof(PixFormat))),
    ]


class RequestBuffers(ctypes.Structure):
    _fields_ = [
        ("count", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("flags", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8 * 3),
    ]


class _Timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _Timecode(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32), ("flags", ctypes.c_uint32),
        ("frames", ctypes.c_uint8), ("seconds", ctypes.c_uint8),
        ("minutes", ctypes.c_uint8), ("hours", ctypes.c_uint8),
        ("userbits", ctypes.c_uint8 * 4),
    ]


class _BufferUnion(ctypes.Union):
    _fields_ = [
        ("offset", ctypes.c_uint32),
        ("userptr", ctypes.c_ulong),
        ("planes", ctypes.c_void_p),
        ("fd", ctypes.c_int32),
    ]


class Buffer(ctypes.Structure):
    """struct v4l2_buffer.

    `timestamp` is a struct timeval, so it is 8-byte aligned and ctypes inserts padding
    after `field` on its own. The trailing padding to 88 bytes is likewise ctypes'
    doing, from the 8-byte alignment of the `m` union. Both are load-bearing; see the
    note on Format above for what happens when a size is wrong.
    """
    _fields_ = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("bytesused", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("timestamp", _Timeval),
        ("timecode", _Timecode),
        ("sequence", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("m", _BufferUnion),
        ("length", ctypes.c_uint32),
        ("reserved2", ctypes.c_uint32),
        ("request_fd", ctypes.c_int32),
    ]

    def timestamp_ns(self) -> int:
        return self.timestamp.tv_sec * 1_000_000_000 + self.timestamp.tv_usec * 1000


class CaptureParm(ctypes.Structure):
    _fields_ = [
        ("capability", ctypes.c_uint32),
        ("capturemode", ctypes.c_uint32),
        ("timeperframe", Fract),
        ("extendedmode", ctypes.c_uint32),
        ("readbuffers", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32 * 4),
    ]


class StreamParm(ctypes.Structure):
    """struct v4l2_streamparm. Every member of its union is 4-byte aligned, so unlike
    Format there is no padding after `type` and the total is 204, not 208."""
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("capture", CaptureParm),
        ("_raw_tail", ctypes.c_uint8 * (200 - ctypes.sizeof(CaptureParm))),
    ]


# ------------------------------------------------------------------------ request numbers

VIDIOC_QUERYCAP = _ior(0, Capability)
VIDIOC_ENUM_FMT = _iowr(2, FmtDesc)
VIDIOC_G_FMT = _iowr(4, Format)
VIDIOC_S_FMT = _iowr(5, Format)
VIDIOC_REQBUFS = _iowr(8, RequestBuffers)
VIDIOC_QUERYBUF = _iowr(9, Buffer)
VIDIOC_QBUF = _iowr(15, Buffer)
VIDIOC_DQBUF = _iowr(17, Buffer)
VIDIOC_STREAMON = _iow(18, ctypes.c_int)
VIDIOC_STREAMOFF = _iow(19, ctypes.c_int)
VIDIOC_G_PARM = _iowr(21, StreamParm)
VIDIOC_S_PARM = _iowr(22, StreamParm)
VIDIOC_ENUM_FRAMESIZES = _iowr(74, FrmSizeEnum)
VIDIOC_ENUM_FRAMEINTERVALS = _iowr(75, FrmIvalEnum)


def ioctl(fd: int, request: int, arg):
    """fcntl.ioctl, with the errno left intact for callers that branch on it.

    Enumeration ioctls signal "no more entries" with EINVAL, and DQBUF signals "nothing
    ready yet" with EAGAIN, so OSError is control flow here rather than a failure.
    """
    return fcntl.ioctl(fd, request, arg)
