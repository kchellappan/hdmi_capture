#!/usr/bin/env python3
"""Pin the ioctl request numbers and structure sizes against the kernel ABI.

An ioctl request number encodes the size of its argument, so a structure that is one byte
wrong does not misbehave subtly -- every call returns ENOTTY and the device looks as
though it does not support the operation. That failure is confusing enough, and far
enough from its cause, to be worth a test that states the expected numbers outright.

The constants below are from the kernel's videodev2.h on x86-64. This is the one place in
the repo where a fact is deliberately written twice: once as a structure definition and
once as the number that definition must produce.
"""
from __future__ import annotations

import ctypes
import sys

import fake  # noqa: F401  - puts the repo root on sys.path

from vcap import v4l2

FAILURES: list[str] = []

SIZES = {
    "v4l2_capability": (v4l2.Capability, 104),
    "v4l2_fmtdesc": (v4l2.FmtDesc, 64),
    "v4l2_frmsizeenum": (v4l2.FrmSizeEnum, 44),
    "v4l2_frmivalenum": (v4l2.FrmIvalEnum, 52),
    "v4l2_format": (v4l2.Format, 208),
    "v4l2_requestbuffers": (v4l2.RequestBuffers, 20),
    "v4l2_buffer": (v4l2.Buffer, 88),
    "v4l2_streamparm": (v4l2.StreamParm, 204),
    "v4l2_pix_format": (v4l2.PixFormat, 48),
}

REQUESTS = {
    "VIDIOC_QUERYCAP": (v4l2.VIDIOC_QUERYCAP, 0x80685600),
    "VIDIOC_ENUM_FMT": (v4l2.VIDIOC_ENUM_FMT, 0xC0405602),
    "VIDIOC_G_FMT": (v4l2.VIDIOC_G_FMT, 0xC0D05604),
    "VIDIOC_S_FMT": (v4l2.VIDIOC_S_FMT, 0xC0D05605),
    "VIDIOC_REQBUFS": (v4l2.VIDIOC_REQBUFS, 0xC0145608),
    "VIDIOC_QUERYBUF": (v4l2.VIDIOC_QUERYBUF, 0xC0585609),
    "VIDIOC_QBUF": (v4l2.VIDIOC_QBUF, 0xC058560F),
    "VIDIOC_DQBUF": (v4l2.VIDIOC_DQBUF, 0xC0585611),
    "VIDIOC_STREAMON": (v4l2.VIDIOC_STREAMON, 0x40045612),
    "VIDIOC_STREAMOFF": (v4l2.VIDIOC_STREAMOFF, 0x40045613),
    "VIDIOC_G_PARM": (v4l2.VIDIOC_G_PARM, 0xC0CC5615),
    "VIDIOC_S_PARM": (v4l2.VIDIOC_S_PARM, 0xC0CC5616),
    "VIDIOC_ENUM_FRAMESIZES": (v4l2.VIDIOC_ENUM_FRAMESIZES, 0xC02C564A),
    "VIDIOC_ENUM_FRAMEINTERVALS": (v4l2.VIDIOC_ENUM_FRAMEINTERVALS, 0xC034564B),
}


def main() -> int:
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        print("  skip  ioctl ABI: the pinned constants are for 64-bit")
        return 0

    for name, (struct, expected) in SIZES.items():
        actual = ctypes.sizeof(struct)
        if actual != expected:
            FAILURES.append(f"sizeof({name}) is {actual}, kernel says {expected}")

    for name, (actual, expected) in REQUESTS.items():
        if actual != expected:
            FAILURES.append(f"{name} is 0x{actual:08x}, kernel says 0x{expected:08x}")

    # The timestamp conversion is the whole reason this module exists; a unit error here
    # would put every frame a thousandfold out and still look plausible.
    buf = v4l2.Buffer()
    buf.timestamp.tv_sec = 12
    buf.timestamp.tv_usec = 345_678
    if buf.timestamp_ns() != 12_345_678_000:
        FAILURES.append(f"timestamp_ns() gave {buf.timestamp_ns()}, want 12345678000")

    if v4l2.fourcc(v4l2.fourcc_to_int("MJPG")) != "MJPG":
        FAILURES.append("fourcc round trip failed")
    # 0x47504a4d is 'MJPG' little-endian, as it appears in a V4L2 format field.
    if v4l2.fourcc_to_int("MJPG") != 0x47504A4D:
        FAILURES.append(f"fourcc_to_int('MJPG') gave 0x{v4l2.fourcc_to_int('MJPG'):08x}")

    for line in FAILURES:
        print(f"  FAIL  {line}")
    if not FAILURES:
        print(f"  ok    {len(SIZES)} struct sizes and {len(REQUESTS)} request numbers "
              f"match the kernel ABI")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
